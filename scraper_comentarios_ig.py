"""
Scraper de comentarios de Instagram — pasada 1 (comentarios raíz)
=================================================================
Extrae los comentarios de publicaciones de Instagram (por URL o por cuenta) y
los guarda deduplicados en <salida>/comentarios.jsonl, con una fila de
auditoría por post en <salida>/estado.csv.

Decisiones de diseño:

  - Extracción por NAVEGADOR (Playwright), no por API. Instagram dejó de servir
    los comentarios por el endpoint REST api/v1/media/{id}/comments/ y los
    mueve por GraphQL con "persisted queries" (doc_id) no documentadas que
    rotan. Reconstruir esas queries a mano es más frágil que dejar que el
    navegador real las arme: se abre el post con la cookie de sesión ya
    inyectada (nunca usuario/contraseña), se scrollea el panel de comentarios y
    se lee el DOM ya renderizado, anclando cada comentario en su <time> (que
    trae el datetime ISO exacto). El descubrimiento de posts por cuenta
    (feed/user) sigue andando por REST.

  - Login = riesgo de BANEO de la cuenta cuya cookie uses, y va contra los
    Términos de Servicio de Instagram. Usá una cuenta DESCARTABLE, delays altos,
    y asumí que la corrida es frágil y de corta vida.

  - Corridas largas y reanudables: cola de trabajo persistida
    (cola_pendiente.json), escritura incremental (cada 250 comentarios y al
    terminar cada post), dedup por `comentario_pk`, y ante un bloqueo se
    RETIRA (persiste la cola y corta) en vez de insistir.

    `comentario_pk` es el ID REAL del comentario, sacado del permalink
    /p/SHORTCODE/c/<id>/ que el DOM expone. Solo si falta se cae a un hash
    sintético de post+autor+texto+timestamp (que fusionaría comentarios
    idénticos del mismo autor).

  - estado.csv: una fila por post y corrida con cuántos comentarios se sacaron,
    cuántos declara Instagram y cómo terminó. Permite auditar la completitud
    sin re-correr. OJO: mirá `cobertura`, no solo `estado`.

Flujo:
  1. Arma la cola: URLs de config.json + posts descubiertos por cuenta.
  2. Para cada post abre el navegador, scrollea y lee los comentarios raíz
     (fase 1). Opcionalmente despliega hilos en el mismo panel (fase 2,
     `incluir_respuestas`), solo recomendable para posts chicos.
  3. `pasada_respuestas.py` (proceso aparte, después) baja las respuestas de los
     hilos abriendo el permalink de cada raíz. Es el camino recomendado para
     las respuestas: trae el padre y el ID numérico del autor explícitos.

Uso:
    python scraper_comentarios_ig.py                  # corre la cola
    python scraper_comentarios_ig.py --estado         # qué posts faltan (no toca IG)
    python scraper_comentarios_ig.py --reset          # reconstruye la cola
    python scraper_comentarios_ig.py --config otro.json --salida datos_otro/

Limitaciones conocidas: ver README.md.
"""

import os
import re
import csv
import sys
import json
import time
import uuid
import random
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
#
# Son globales del módulo para que pasada_respuestas.py y las pruebas las lean
# (y las puedan redirigir). Se fijan con `configurar_rutas()`; los valores de
# abajo son los defaults, relativos a la carpeta del script.

BASE_DIR = Path(__file__).parent
CONFIG_FILE  = BASE_DIR / "config.json"
SECRETS_FILE = BASE_DIR / "secrets.json"          # cookie de sesión (gitignored)


def configurar_rutas(salida: Path | str | None = None, crear: bool = True) -> None:
    """Fija la carpeta de salida y las rutas derivadas (y crea las carpetas)."""
    global DATA_DIR, LOG_DIR, CAPTURAS_DIR
    global COMENTARIOS_FILE, POSTS_FILE, ESTADO_FILE, COLA_FILE
    DATA_DIR = Path(salida) if salida else BASE_DIR / "data"
    LOG_DIR  = DATA_DIR / "logs"
    CAPTURAS_DIR = DATA_DIR / "capturas"           # una imagen del post por shortcode
    COMENTARIOS_FILE = DATA_DIR / "comentarios.jsonl"     # output principal (dedup por pk)
    POSTS_FILE       = DATA_DIR / "posts.csv"             # posts descubiertos por cuenta
    ESTADO_FILE      = DATA_DIR / "estado.csv"            # auditoría: una fila por post
    COLA_FILE        = DATA_DIR / "cola_pendiente.json"   # cola de trabajo (resume)
    if crear:
        for d in (DATA_DIR, LOG_DIR, CAPTURAS_DIR):
            d.mkdir(exist_ok=True, parents=True)


configurar_rutas(crear=False)       # importar el módulo no crea carpetas

# ID público de la app web de Instagram. Es una constante conocida que el
# frontend manda en todas sus llamadas; sin este header la API interna
# responde 403. No es un secreto ni identifica a tu cuenta.
IG_APP_ID = "936619743392459"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Logging (UTF-8: la consola de Windows en cp1252 crashea con → / ⚠)
# ---------------------------------------------------------------------------

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("scraper_ig")


def configurar_logging(prefijo: str = "ig") -> None:
    """Consola + archivo en <salida>/logs/. Se llama desde main(), no al importar."""
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for h in (
        logging.FileHandler(LOG_DIR / f"{prefijo}_{datetime.now():%Y%m%d_%H%M%S}.log",
                            encoding="utf-8"),
        logging.StreamHandler(),
    ):
        h.setFormatter(fmt)
        log.addHandler(h)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "cuentas": [],                     # ["usuario1", "usuario2", ...]
    "post_urls": [],                   # ["https://www.instagram.com/p/XXXX/", ...]
    "max_posts_por_cuenta": 20,        # None = todos
    "max_comentarios_por_post": None,  # None = todos
    "max_minutos_por_post": None,      # None = sin límite de tiempo
    "incluir_respuestas": False,       # paginar hilos de respuestas (más lento)
    "delay_entre_requests": [3.0, 7.0],
    "delay_entre_posts": [5.0, 12.0],
    "max_reintentos": 3,               # por request transitorio (timeout/5xx)
}

# Estados de `estado.csv` que cuentan como post RESUELTO para el resume: un
# post con alguno de estos no se vuelve a scrapear. Todo lo demás
# (ok_truncado_por_ig, ok_corte_tiempo, ok_corte_seguridad, error_inesperado)
# vuelve a la cola.
#
# `ok_tope` cuenta como resuelto porque no es una truncación de Instagram sino
# un corte pedido por `max_comentarios_por_post`: el post terminó como se
# quería. Sin esto, con el flujo de acumular URLs en config.json, cada post que
# tocó el tope se reabría y se volvía a scrollear en cada corrida — gastando de
# más justo donde el tope existe para ahorrar. (Contra: si después querés ese
# mismo post completo, hay que sacarlo de estado.csv o correr con --reset.)
#
# `ok_corte_tiempo` NO entra: un post cortado por reloj se reintenta, y el
# dedup por pk hace que el reintento solo agregue lo que falta.
ESTADOS_RESUELTOS = frozenset({"ok_completo", "ok_tope"})


def cargar_config(ruta: Path | str | None = None) -> dict:
    ruta = Path(ruta) if ruta else CONFIG_FILE
    cfg = dict(DEFAULTS)
    if ruta.exists():
        cfg.update(json.loads(ruta.read_text(encoding="utf-8")))
    else:
        log.warning(f"No existe {ruta}; usando defaults. Ver config.example.json")
    return cfg


def cargar_sesion() -> dict:
    """
    Devuelve las cookies de sesión. Se leen de (en orden de prioridad):
      1. variables de entorno IG_SESSIONID / IG_CSRFTOKEN / IG_DS_USER_ID /
         IG_MID / IG_DID
      2. secrets.json  ->  {"sessionid": "...", "csrftoken": "...", "ds_user_id": "..."}
    NUNCA se hardcodean acá. secrets.json va en .gitignore.
    """
    ses = {}
    if SECRETS_FILE.exists():
        ses.update(json.loads(SECRETS_FILE.read_text(encoding="utf-8")))
    for env_key, cookie_key in (
        ("IG_SESSIONID", "sessionid"),
        ("IG_CSRFTOKEN", "csrftoken"),
        ("IG_DS_USER_ID", "ds_user_id"),
        ("IG_MID", "mid"),
        ("IG_DID", "ig_did"),
    ):
        if os.environ.get(env_key):
            ses[cookie_key] = os.environ[env_key]

    if not ses.get("sessionid"):
        log.error(
            "Falta la cookie de sesión. Poné IG_SESSIONID en el entorno o creá "
            "secrets.json con {\"sessionid\": \"...\", \"csrftoken\": \"...\"}. "
            "Ver README, sección «Sesión»."
        )
        sys.exit(1)
    return ses

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class BloqueoInstagram(Exception):
    """Se levantó un bloqueo (login_required / challenge / 429). Cortar."""


def crear_sesion_http(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
        "X-IG-App-ID": IG_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
    })
    if cookies.get("csrftoken"):
        s.headers["X-CSRFToken"] = cookies["csrftoken"]
    for k, v in cookies.items():
        if v:
            s.cookies.set(k, v, domain=".instagram.com")
    return s


def delay(rango):
    time.sleep(random.uniform(*rango))


def get_json(sesion: requests.Session, url: str, cfg: dict, params: dict | None = None) -> dict:
    """
    GET con reintentos acotados y detección de bloqueo.
      - 200 + JSON  -> lo devuelve.
      - 401/403 con login_required/challenge, o 429 -> BloqueoInstagram (fatal).
      - 5xx / timeout / JSON inválido -> reintenta con backoff hasta max_reintentos.
    """
    max_reintentos = cfg["max_reintentos"]
    for intento in range(1, max_reintentos + 1):
        try:
            resp = sesion.get(url, params=params, timeout=25)
        except requests.RequestException as e:
            espera = 2 ** intento
            log.warning(f"  Error de red ({e}); reintento {intento}/{max_reintentos} en {espera}s")
            time.sleep(espera)
            continue

        # --- Bloqueos: cortar la corrida entera (regla: retirarse) ---
        if resp.status_code == 429:
            raise BloqueoInstagram("HTTP 429 (rate limit). Esperá varias horas antes de retomar.")
        if resp.status_code in (401, 403):
            cuerpo = resp.text[:300].lower()
            if "login_required" in cuerpo or "challenge" in cuerpo or "checkpoint" in cuerpo:
                raise BloqueoInstagram(
                    f"HTTP {resp.status_code} con login/challenge. La cookie caducó o "
                    f"la cuenta fue marcada. NO reintentar en caliente."
                )
            # 403 sin señal clara: puede ser header/app-id; lo tratamos como fatal
            raise BloqueoInstagram(f"HTTP {resp.status_code}: {resp.text[:200]}")

        if resp.status_code >= 500:
            espera = 2 ** intento
            log.warning(f"  HTTP {resp.status_code}; reintento {intento}/{max_reintentos} en {espera}s")
            time.sleep(espera)
            continue

        # A veces IG devuelve HTML (página de login/consent) con 200.
        try:
            return resp.json()
        except ValueError:
            if "loginForm" in resp.text or "<html" in resp.text[:200].lower():
                raise BloqueoInstagram(
                    "La API devolvió HTML en vez de JSON (probable login/consent). "
                    "Revisá la cookie de sesión."
                )
            espera = 2 ** intento
            log.warning(f"  Respuesta no-JSON; reintento {intento}/{max_reintentos} en {espera}s")
            time.sleep(espera)
            continue

    raise RuntimeError(f"Agotados {max_reintentos} reintentos para {url}")

# ---------------------------------------------------------------------------
# Helpers de datos
# ---------------------------------------------------------------------------

ALFABETO_SHORTCODE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def shortcode_a_media_id(shortcode: str) -> int:
    """
    El shortcode de una URL (/p/XXXX/) es el media id codificado en base64
    con este alfabeto. Se decodifica a entero = media id, que es lo que
    pide el endpoint de comentarios. Evita una llamada extra.
    """
    media_id = 0
    for c in shortcode:
        media_id = media_id * 64 + ALFABETO_SHORTCODE.index(c)
    return media_id


def shortcode_de_url(url: str) -> str | None:
    """Extrae el shortcode de una URL de post/reel de Instagram."""
    partes = [p for p in url.split("/") if p]
    for i, p in enumerate(partes):
        if p in ("p", "reel", "tv") and i + 1 < len(partes):
            return partes[i + 1]
    return None


def ts_a_iso(epoch) -> str | None:
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None

# ---------------------------------------------------------------------------
# Descubrimiento de posts de una cuenta
# ---------------------------------------------------------------------------

def obtener_user_id(sesion, username: str, cfg: dict) -> str | None:
    """web_profile_info: username -> user id (+ metadata de la cuenta)."""
    data = get_json(
        sesion,
        "https://www.instagram.com/api/v1/users/web_profile_info/",
        cfg,
        params={"username": username},
    )
    user = (data.get("data") or {}).get("user")
    if not user:
        log.warning(f"  No se encontró el usuario '{username}' (¿existe? ¿privado sin follow?)")
        return None
    if user.get("is_private") and not user.get("followed_by_viewer"):
        log.warning(f"  '{username}' es privada y la cuenta logueada no la sigue: se salta.")
        return None
    return user.get("id")


def descubrir_posts(sesion, username: str, cfg: dict) -> list[dict]:
    """
    Lista las publicaciones de una cuenta paginando el endpoint feed/user.
    Devuelve dicts con: shortcode, media_id, caption, comment_count, taken_at.
    Respeta max_posts_por_cuenta.
    """
    user_id = obtener_user_id(sesion, username, cfg)
    if not user_id:
        return []

    max_posts = cfg["max_posts_por_cuenta"]
    posts, next_max_id = [], None

    while True:
        params = {"count": 33}
        if next_max_id:
            params["max_id"] = next_max_id
        data = get_json(
            sesion,
            f"https://www.instagram.com/api/v1/feed/user/{user_id}/",
            cfg,
            params=params,
        )
        items = data.get("items") or []
        for it in items:
            posts.append({
                "cuenta": username,
                "shortcode": it.get("code"),
                "media_id": str(it.get("pk")),
                "caption": ((it.get("caption") or {}) or {}).get("text"),
                "comment_count": it.get("comment_count"),
                "taken_at": ts_a_iso(it.get("taken_at")),
            })
            if max_posts and len(posts) >= max_posts:
                return posts

        if not data.get("more_available"):
            break
        next_max_id = data.get("next_max_id")
        if not next_max_id:
            break
        delay(cfg["delay_entre_requests"])

    return posts

# ---------------------------------------------------------------------------
# Comentarios de un post (via navegador — ver v2 en el changelog del módulo)
# ---------------------------------------------------------------------------
#
# Instagram sirve la vista de un post (y sus comentarios) por GraphQL con
# "persisted queries" (doc_id) no documentadas, y dejó de responder al viejo
# endpoint REST api/v1/media/{id}/comments/ con JSON (ahora devuelve el HTML
# de la SPA). Reconstruir esas queries GraphQL a mano es frágil porque los
# doc_id rotan. En cambio, se deja que el navegador real arme esas llamadas
# (imposible de replicar mejor que el propio cliente) y se lee el resultado
# ya renderizado en el DOM, anclando cada comentario en su elemento <time>
# (trae datetime ISO exacto en el atributo, no solo el texto relativo tipo
# "7 h"). Cada comentario procesado se marca con data-scrp="1" para no
# reprocesarlo en el próximo scroll.

# Las respuestas de un hilo se distinguen de los comentarios raíz por la
# INDENTACIÓN: su bloque arranca ~44px más a la derecha. Se usa eso para
# armar la jerarquía (el padre es el bloque no-indentado anterior más
# cercano en orden de documento). Cada <time> recibe un id secuencial
# estable (data-scrp-id) para poder referenciar al padre entre pasadas,
# porque en la pasada siguiente el padre ya está marcado y no se re-emite.
# La posición x se cachea en el nodo: recalcular getBoundingClientRect de
# miles de elementos en cada pasada es carísimo.
# El panel de comentarios tiene su propio scroll (no scrollea la página
# entera). OJO: no alcanza con buscar scrollHeight > clientHeight — hay
# ancestros que cumplen eso pero NO son scrolleables (asignarles scrollTop no
# hace nada y el scroll queda clavado en 0). Hay que filtrar por overflowY
# auto|scroll, y entre los candidatos quedarse con el que más <time> contiene,
# que es el de los comentarios y no algún otro panel de la página.
#
# Se cachea en window porque repetir la búsqueda en cada iteración cuesta
# getComputedStyle + scrollHeight (reflow) + querySelectorAll sobre cada div
# del documento: con miles de comentarios cargados eso es O(n²) y clavaba la
# corrida. Pero la RE-BÚSQUEDA ante un cache muerto es igual de importante:
# Instagram re-renderiza y reemplaza el nodo, y seguir apuntando al viejo hacía
# que no se viera ningún comentario y que el scroll fallara, cortando con un
# falso "ok_completo".
_JS_CONT_HELPER = """
    function panelComentarios() {
        let el = window.__scrpCont;
        if (el && document.contains(el)) return el;
        let mejor = null, mejorN = -1;
        for (const c of document.querySelectorAll("div, ul, section, main")) {
            const oy = getComputedStyle(c).overflowY;
            if ((oy === "auto" || oy === "scroll") && c.scrollHeight > c.clientHeight + 20) {
                const n = c.querySelectorAll("time").length;
                if (n > mejorN) { mejorN = n; mejor = c; }
            }
        }
        window.__scrpCont = mejor;
        return mejor;
    }
"""

_JS_EXTRAER_COMENTARIOS = "(modo) => {" + _JS_CONT_HELPER + """
    // De dónde se leen los <time>, y por qué es un parámetro y no una constante:
    //
    // modo "panel" (panel del post): solo los <time> DENTRO del panel de
    // comentarios. El post tiene su propio <time> en el encabezado, con una x
    // menor que la de los comentarios: si se cuela, ensucia el umbral de
    // indentación y toda la clasificación raíz/respuesta sale mal. Se consulta
    // el subárbol del panel directamente (no document + filtro): filtrar con
    // contains() cuesta O(n · profundidad) en cada pasada.
    //
    // modo "documento" (página de permalink): ahí el panel scrolleable NO
    // contiene el hilo. Medido 2026-07-28: el panel elegido tenía 16 <time> y el
    // documento 50 — o sea las respuestas quedaban fuera del subárbol y la
    // extracción devolvía solo los raíz por más que el hilo estuviera abierto.
    // Leer todo el documento es seguro desde que el umbral se calcula solo sobre
    // bloques con "Responder": el bloque del post ya no lo contamina.
    const raiz = (modo === "documento") ? document : (panelComentarios() || document);
    const times = Array.from(raiz.querySelectorAll("time"));
    const info = [];
    for (let i = 0; i < times.length; i++) {
        const t = times[i];
        const yaVisto = t.hasAttribute("data-scrp");
        const x = t.hasAttribute("data-scrp-x") ? parseInt(t.getAttribute("data-scrp-x"), 10) : null;
        let contenedor = null;
        if (!yaVisto) {
            // El bloque de un comentario es el ancestro MÁS GRANDE que sigue
            // conteniendo un solo <time>: el suyo. En cuanto un ancestro
            // abarca el <time> vecino, ya nos pasamos al contenedor de la
            // lista. Se detecta con contains(), que es O(profundidad) y no
            // toca layout. (Antes se leía innerText de cada ancestro: en los
            // ancestros altos eso serializa TODA la lista de comentarios, y
            // con miles cargados la corrida se arrastraba a paso de tortuga.)
            const prev = i > 0 ? times[i - 1] : null;
            const next = i + 1 < times.length ? times[i + 1] : null;
            contenedor = t.parentElement;
            let anc = t.parentElement;
            for (let k = 0; k < 30 && anc; k++) {
                if ((prev && anc.contains(prev)) || (next && anc.contains(next))) break;
                contenedor = anc;
                anc = anc.parentElement;
            }
        }
        info.push({t, contenedor, x, yaVisto, texto: null, href: null});
    }

    // Layout: primero TODAS las lecturas, después TODAS las escrituras.
    // Intercalarlas obliga al navegador a recalcular el layout en cada
    // elemento (layout thrashing), y con un DOM grande eso se nota.
    // OJO: innerText TAMBIÉN es una lectura de layout, no solo
    // getBoundingClientRect. Leerlo abajo, después de los setAttribute, hacía
    // que cada comentario nuevo disparara un reflow del documento entero: con
    // ~7500 comentarios cargados la fase 2 pasaba horas sin avanzar, el
    // renderer clavado al 100% de CPU. Las dos lecturas van acá, juntas y
    // antes de escribir nada.
    for (const it of info) {
        if (it.yaVisto || !it.contenedor) continue;
        if (it.x === null) it.x = Math.round(it.contenedor.getBoundingClientRect().x);
        it.texto = it.contenedor.innerText;
        // Permalink del comentario: el <time> va envuelto en un
        // <a href="/p/SHORTCODE/c/<id>/">. De ahí sale el ID REAL de IG, que
        // reemplaza al comentario_pk sintético (hash del contenido). closest()
        // recorre el árbol, no toca layout, así que va con las otras lecturas.
        const a = it.t.closest('a[href*="/c/"]');
        it.href = a ? a.getAttribute("href") : null;

        // Imágenes del bloque, SIN el avatar del autor (que está en todos los
        // comentarios y haría inútil la señal). Lo que queda es sticker o GIF:
        // es lo que distingue un comentario con imagen y sin texto de un fallo
        // de parseo — hoy los dos casos son un texto vacío indistinguible.
        // Se guardan los alt crudos: la taxonomía se fija después, mirando
        // casos reales, no adivinando acá.
        const alts = [];
        for (const im of it.contenedor.querySelectorAll("img[alt]")) {
            const alt = im.getAttribute("alt") || "";
            if (/^(Foto del perfil de|Profile picture|.+'s profile picture)/i.test(alt)) continue;
            alts.push(alt);
        }
        it.alts = alts;
        // Badge de verificado. Venía null en el 100% de las filas porque no se
        // consultaba: está en el DOM. querySelector recorre el árbol y NO
        // fuerza layout (la trampa de performance era innerText), pero va acá
        // igual, en el bloque de lecturas, antes de cualquier setAttribute.
        it.verificado = !!it.contenedor.querySelector(
            'svg[aria-label="Verificado"], svg[aria-label="Verified"], ' +
            '[title="Verificado"], [title="Verified"]'
        );
    }

    // El umbral se calcula SOLO sobre bloques que son comentarios de verdad.
    // En la página de permalink el bloque del POST también tiene <time>, y su x
    // es ~16px menor que la de los comentarios: si entra en el mínimo, el umbral
    // baja lo suficiente como para que las RAÍCES caigan del lado "respuesta".
    // Entonces ninguna respuesta encuentra padre (el único bloque por encima del
    // umbral es el del post) y la pasada 2 devuelve CERO respuestas siempre.
    // Medido 2026-07-28: minX=716 por el bloque del post, umbral 726, y 49 de 50
    // bloques clasificados como respuesta con padre nulo. Con el filtro: minX=732,
    // umbral 742, 32 respuestas todas con su padre correcto.
    // La señal de "esto es un comentario" es la misma que usa el parser: una
    // línea que termina en "Responder". En el panel del post no cambia nada,
    // porque ahí todos los bloques la tienen.
    let minX = null;
    for (const it of info) {
        if (it.x === null) continue;
        if (!/Responder[ \\t]*$/m.test(it.texto || "")) continue;
        if (minX === null || it.x < minX) minX = it.x;
    }
    // Si NINGÚN bloque parece comentario (pasada que agarró el DOM a medio
    // renderizar), se cae al criterio viejo en vez de dejar el umbral en 10 y
    // clasificar todo como respuesta.
    if (minX === null) {
        for (const it of info) {
            if (it.x !== null && (minX === null || it.x < minX)) minX = it.x;
        }
    }
    const umbral = (minX === null ? 0 : minX) + 10;

    // A partir de acá, solo escrituras: ya no se vuelve a leer layout.
    let seq = parseInt(document.body.getAttribute("data-scrp-seq") || "0", 10);
    for (const it of info) {
        if (it.x !== null && !it.t.hasAttribute("data-scrp-x")) {
            it.t.setAttribute("data-scrp-x", String(it.x));
        }
        if (!it.t.hasAttribute("data-scrp-id")) it.t.setAttribute("data-scrp-id", String(seq++));
    }
    document.body.setAttribute("data-scrp-seq", String(seq));

    const out = [];
    for (let i = 0; i < info.length; i++) {
        const it = info[i];
        if (it.yaVisto || !it.contenedor || it.x === null) continue;
        it.t.setAttribute("data-scrp", "1");
        const esRespuesta = it.x > umbral;
        let padreId = null;
        if (esRespuesta) {
            for (let j = i - 1; j >= 0; j--) {
                if (info[j].x !== null && info[j].x <= umbral) {
                    padreId = info[j].t.getAttribute("data-scrp-id");
                    break;
                }
            }
        }
        const lineas = (it.texto || "").split("\\n").map(l => l.trim()).filter(l => l.length > 0);
        out.push({
            lineas,
            tiempo_iso: it.t.getAttribute("datetime"),
            id: it.t.getAttribute("data-scrp-id"),
            padre_id: padreId,
            es_respuesta: esRespuesta,
            href: it.href,
            // Insumos de la decisión raíz/respuesta. Se devuelven para poder
            // auditarla después: el umbral se calcula POR PASADA sobre los
            // bloques visibles en esa pasada, así que una pasada que solo
            // contenga respuestas toma como minX la x de una respuesta y
            // clasifica todo como raíz. Sin estos campos eso es invisible.
            x: it.x,
            umbral: umbral,
            min_x: minX,
            alts: it.alts || [],
            verificado: !!it.verificado,
        });
    }
    return out;
}"""

# Despliega hilos. Los tres botones posibles, y en qué se diferencian:
#   - "Ver las N respuestas"  (panel del post)   → abre el hilo, trae ~12 de una
#   - "Ver respuestas (N)"    (permalink)        → ES EL PAGINADOR: trae 3 y el
#     botón QUEDA, descontando ("(24)" → "(21)" → ...). Medido en vivo 2026-07-28.
#   - "Mostrar más respuestas" (panel del post)  → paginador de texto FIJO
#
# De ahí la regla de marcado, que es la parte delicada (ya se falló para los dos
# lados):
#   - Botón CON contador → se marca con el TEXTO clickeado, no con "ya lo
#     clickeé". Si en la pasada siguiente el contador bajó, es progreso y se
#     vuelve a clickear; si no se movió durante `MAX_ESTANCADO` pasadas, el hilo
#     está agotado y recién ahí se lo deja. Marcarlo por nodo (lo que hacía
#     `data-scrp-exp="1"`) bajaba 3 respuestas por hilo y no volvía nunca.
#   - Botón SIN contador → NO se marca (no hay forma de saber si avanzó mirando
#     el DOM), así que se re-clickea siempre. Lo que impide el loop infinito es
#     que el llamador cuenta el progreso en COMENTARIOS NUEVOS y no en clicks:
#     contar clicks fue el bug del 22/7 (2,5 h clickeando sin bajar nada).
# Se tolera que el contador no se mueva en la primera pasada porque Instagram a
# veces tarda en responder el click; lo que no se tolera es que no se mueva nunca.
#
# Se busca dentro del panel de comentarios (no en todo el documento) y se lee
# textContent, no innerText: innerText fuerza reflow en cada nodo.
_JS_EXPANDIR_RESPUESTAS = "({limite, modo, ancla}) => {" + _JS_CONT_HELPER + """
    const MAX_ESTANCADO = 3;
    // Mismo criterio que la extracción: en el permalink el botón puede estar
    // fuera del panel scrolleable, así que se busca en todo el documento.
    const raiz = (modo === "documento") ? document.body : (panelComentarios() || document.body);
    const re = /^(Ver (la|las) [\\d.,]+ respuestas?|Ver respuestas? \\([\\d.,]+\\)|Mostrar más respuestas)$/;
    // `ancla` = id del comentario cuyo hilo interesa. La página del permalink
    // muestra también los otros comentarios del post, cada uno con SU botón, y
    // clickearlos dispara requests de hilos que no vamos a guardar (y que
    // quedarían incompletos). Con ancla se considera únicamente el primer botón
    // que aparece DESPUÉS del bloque del comentario enfocado, que es el suyo.
    // Cuando ese botón desaparece (hilo agotado), no queda ninguno y el
    // llamador corta en vez de seguir abriendo hilos ajenos.
    let desde = null, hasta = null;
    if (ancla) {
        desde = document.querySelector('a[href*="/c/' + ancla + '/"]');
        if (!desde) return {clicks: 0, ver: 0, pendientes: 0, agotados: 0, ancla_ok: false};
        // El rango termina en el permalink del SIGUIENTE comentario del post.
        // Sin ese tope, cuando el botón del hilo desaparece (hilo agotado) el
        // "primer botón después del ancla" pasa a ser el del vecino y la pasada
        // se pone a abrir hilos ajenos en vez de cortar.
        // Los permalinks de las respuestas del propio hilo (/c/<ancla>/r/<id>)
        // no cuentan como siguiente comentario: son parte de este hilo.
        let visto = false;
        for (const a of document.querySelectorAll('a[href*="/c/"]')) {
            if (a === desde) { visto = true; continue; }
            if (!visto) continue;
            if ((a.getAttribute("href") || "").indexOf("/c/" + ancla + "/") !== -1) continue;
            hasta = a;
            break;
        }
    }
    const els = raiz.querySelectorAll("span, div[role=button]");
    let n = 0, pendientes = 0, ver = 0, agotados = 0;
    for (const el of els) {
        if (el.children.length !== 0) continue;
        const txt = (el.textContent || "").trim();
        if (!re.test(txt)) continue;
        if (desde) {
            const pos = desde.compareDocumentPosition(el);
            if (!(pos & Node.DOCUMENT_POSITION_FOLLOWING)) continue;   // está antes del ancla
            if (hasta && !(hasta.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING)) {
                continue;                                              // ya es del comentario siguiente
            }
        }
        const conContador = /[\\d]/.test(txt);
        const firma = el.getAttribute("data-scrp-exp");
        let agotado = false;
        if (conContador) {
            if (firma === txt) {
                // El contador no se movió desde el último click.
                const estancado = parseInt(el.getAttribute("data-scrp-estancado") || "0", 10) + 1;
                el.setAttribute("data-scrp-estancado", String(estancado));
                if (estancado >= MAX_ESTANCADO) { agotados++; agotado = true; }
            } else if (firma !== null) {
                el.setAttribute("data-scrp-estancado", "0");   // bajó: sigue sirviendo
            }
        }
        if (!agotado) {
            pendientes++;
            if (n < limite) {
                if (conContador) {
                    if (firma === null) ver++;      // hilo abierto por primera vez
                    el.setAttribute("data-scrp-exp", txt);
                }
                try { el.click(); n++; } catch (e) {}
            }
        }
        // Con ancla solo interesa el PRIMER botón que sigue al comentario
        // enfocado: los que vienen después son de otros comentarios del post.
        if (desde) break;
    }
    // `ver` se sigue devolviendo para el log (hilos abiertos por primera vez),
    // pero NO sirve como criterio de corte: el paginador con contador reaparece
    // pasada tras pasada. El corte lo decide el llamador con los comentarios
    // nuevos extraídos.
    return {clicks: n, ver: ver, pendientes: pendientes, agotados: agotados, ancla_ok: true};
}"""

# Localiza el panel de comentarios desde cero (ver panelComentarios()).
_JS_INIT_CONTENEDOR = "() => {" + _JS_CONT_HELPER + """
    window.__scrpCont = null;              // forzar búsqueda limpia por post
    const el = panelComentarios();
    return {ok: !!el, n_times: el ? el.querySelectorAll("time").length : 0};
}"""

_JS_SCROLL_CONTENEDOR = "() => {" + _JS_CONT_HELPER + """
    const el = panelComentarios();
    if (!el) return {ok: false};
    const antes = el.scrollTop;
    el.scrollTop = el.scrollHeight;
    return {ok: true, avanzo: el.scrollTop > antes};
}"""

_RE_LIKES = re.compile(r"^([\d.,]+)\s*Me gusta$")
# Likes embebidos en una línea mashed ("2 d349 Me gustaResponder") — ver parser.
_RE_LIKES_INLINE = re.compile(r"([\d.,]+)\s*Me gusta", re.IGNORECASE)
# Contador de respuestas en sus DOS formas: "Ver las 6 respuestas" (panel del
# post) y "Ver respuestas (6)" (página de permalink del comentario).
_RE_RESPUESTAS = re.compile(
    r"Ver (?:la|las) (\d+) respuestas?|Ver respuestas? \((\d+)\)", re.IGNORECASE)
# Tiempo relativo de IG en español ("2 d", "3 h", "5 min", "1 sem", "2 a").
# En el layout del post viene en su propia línea; se descarta (ya tenemos el
# datetime exacto del <time>).
_RE_RELTIME = re.compile(
    r"^\d+\s*(?:s|seg|min|h|d|sem|a|años?|semanas?|d[ií]as?|horas?|minutos?|segundos?)$",
    re.IGNORECASE)
# El ID real del comentario sale del permalink /p/SHORTCODE/c/<id>/.
_RE_COMMENT_ID = re.compile(r"/c/(\d+)")


def _reply_count_de_linea(linea: str) -> int:
    m = _RE_RESPUESTAS.search(linea)
    if not m:
        return 0
    return int(m.group(1) or m.group(2))

# Métricas del post en sí: Instagram las pone en el <meta name="description">,
# con la forma "74K likes, 8,679 comments - <cuenta> el July 20, 2026".
# Ojo: los likes vienen REDONDEADOS ("74K"); los comentarios sí son exactos.
# Por eso se guarda además el string crudo, para no perder el dato original.
_RE_META_LIKES = re.compile(r"([\d.,]+)\s*([KMB])?\s*(?:likes|me gusta)", re.IGNORECASE)
_RE_META_COMENTARIOS = re.compile(r"([\d.,]+)\s*([KMB])?\s*(?:comments|comentarios)", re.IGNORECASE)

_JS_META_POST = """
() => {
    const m = document.querySelector('meta[property="og:description"]')
          || document.querySelector('meta[name="description"]');
    const url = document.querySelector('meta[property="og:url"]');
    return {
        descripcion: m ? m.getAttribute("content") : null,
        og_url: url ? url.getAttribute("content") : null,
    };
}
"""


# Hora de publicación del post. NO sale del meta description: ese string da
# "el July 21, 2026" — día sin hora y con el mes en inglés. Sale del <time> del
# ENCABEZADO del post, que sí trae el datetime ISO exacto.
#
# Va en un evaluate APARTE, sin tocar _JS_EXTRAER_COMENTARIOS: ese JS excluye a
# propósito el <time> del encabezado, porque su x es menor que la de los
# comentarios y ensuciaría el umbral de indentación.
#
# Para elegir cuál <time> es el del post se descartan los que están dentro del
# panel de comentarios y, entre los que quedan, se toma el MÁS VIEJO: un
# comentario no puede ser anterior a su post. El criterio sobrevive a que el
# panel no se haya encontrado (posts chicos, sin contenedor scrolleable), que
# es justo el caso donde filtrar por panel no alcanza.
_JS_TIEMPO_POST = "() => {" + _JS_CONT_HELPER + """
    const panel = panelComentarios();
    const fuera = [], todos = [];
    for (const t of document.querySelectorAll("time[datetime]")) {
        const dt = t.getAttribute("datetime");
        if (!dt) continue;
        todos.push(dt);
        if (!panel || !panel.contains(t)) fuera.push(dt);
    }
    const cand = fuera.length ? fuera : todos;
    if (!cand.length) return {taken_at: null, n_candidatos: 0, fuera_del_panel: fuera.length};
    cand.sort();
    return {taken_at: cand[0], n_candidatos: cand.length, fuera_del_panel: fuera.length};
}"""

# El caption viaja dentro del meta description, después de la fecha y entre
# comillas: '... - <cuenta> el July 20, 2026: "texto del caption"'.
# Instagram lo TRUNCA en captions largos; se acepta, es la única fuente barata.
#
# OJO con el cierre: Instagram agrega un PUNTO después de la comilla final, o
# sea el string termina en '". ' y no en '"'. Exigir la comilla como último
# carácter (\s*$) devolvía None con el caption ahí adelante. Por eso [\s.]*$.
# El (.*) greedy ancla en la ÚLTIMA comilla, así que un caption con comillas
# rectas adentro sobrevive entero.
_RE_META_CAPTION = re.compile(r':\s*"(.*)"[\s.]*$', re.DOTALL)


def _caption_de_meta(desc: str | None) -> str | None:
    if not desc:
        return None
    m = _RE_META_CAPTION.search(desc.strip())
    if not m:
        return None
    cap = m.group(1).strip()
    return cap or None


def _edad_post_horas(taken_at: str | None, ahora: datetime) -> float | None:
    """Horas entre la publicación del post y el momento de la captura."""
    if not taken_at:
        return None
    try:
        pub = datetime.fromisoformat(taken_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return round((ahora - pub).total_seconds() / 3600, 2)


def _num_meta(match) -> int | None:
    """Convierte "8,679" -> 8679 y "74K" -> 74000 (aproximado, IG lo redondea)."""
    if not match:
        return None
    crudo, sufijo = match.group(1), match.group(2)
    try:
        if sufijo:
            # Con sufijo el número es decimal ("1.2K"); la coma sería de miles.
            valor = float(crudo.replace(",", ""))
            return int(valor * {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[sufijo.upper()])
        return int(crudo.replace(".", "").replace(",", ""))
    except (ValueError, KeyError):
        return None


def extraer_meta_post(page, post: dict) -> dict:
    """
    Métricas del post en el momento del scrapeo (likes y total de comentarios
    que declara Instagram). Sirven para auditar completitud: cuántos
    comentarios dice IG que hay vs cuántos pudimos extraer.
    """
    meta = page.evaluate(_JS_META_POST)
    tiempo = page.evaluate(_JS_TIEMPO_POST)       # evaluate aparte: ver _JS_TIEMPO_POST
    desc = meta.get("descripcion") or ""
    url = meta.get("og_url") or construir_url_post(post).split("?")[0]
    # og:url tiene la forma https://www.instagram.com/<cuenta>/p/<shortcode>/,
    # así que sirve para completar la cuenta en modo post_urls (donde no la
    # sabemos de antemano).
    partes = [p for p in url.split("/") if p]
    cuenta = None
    for i, p in enumerate(partes):
        if p == "p" and i > 0 and partes[i - 1] != "instagram.com":
            cuenta = partes[i - 1]
            break
    taken_at = tiempo.get("taken_at")
    if not taken_at:
        log.warning("  ⚠ No se pudo leer la hora de publicación del post "
                    "(sin <time> en el encabezado): no hay edad_post_horas ni "
                    "normalización temporal para este post.")
    return {
        "post_like_count": _num_meta(_RE_META_LIKES.search(desc)),
        "post_comment_count": _num_meta(_RE_META_COMENTARIOS.search(desc)),
        "post_meta_crudo": desc or None,          # texto original, sin interpretar
        "caption": post.get("caption") or _caption_de_meta(desc),
        "taken_at": taken_at,
        "edad_post_horas": _edad_post_horas(taken_at, datetime.now(tz=timezone.utc)),
        "url": url,
        "cuenta": post.get("cuenta") or cuenta,
    }


def guardar_captura_post(page, post: dict) -> str | None:
    """
    Captura de la imagen del post (data/capturas/<shortcode>.png). Se saca acá
    porque la página YA está abierta: no cuesta ni una navegación extra contra
    Instagram, mientras que hacerlo después costaría una carga por post.

    Sale del viewport, no de la página entera: en un post con miles de
    comentarios `full_page=True` serializa una imagen de decenas de miles de
    píxeles de alto. Se llama ANTES de scrollear, así que el viewport es
    justamente el encabezado con la imagen del post.

    Nunca levanta: una captura fallida no puede costar la corrida.
    Si el archivo ya existe no se rehace — al retomar un post cortado, la
    captura buena es la de la primera pasada.
    """
    shortcode = post.get("shortcode") or post.get("code")
    if not shortcode:
        return None
    destino = CAPTURAS_DIR / f"{shortcode}.png"
    if destino.exists():
        return str(destino)
    try:
        page.screenshot(path=str(destino), timeout=10000)
        return str(destino)
    except Exception as e:
        log.warning(f"  ⚠ No se pudo guardar la captura del post: {e}")
        return None


def _parsear_bloque_comentario(lineas: list[str], tiempo_iso: str | None, post: dict,
                               parent_pk: str | None = None,
                               href: str | None = None,
                               posicion_dom: int | None = None,
                               carga_id: str | None = None,
                               fase: int | None = None,
                               origen: str | None = None,
                               x_bloque: int | None = None,
                               umbral_indentacion: int | None = None,
                               min_x: int | None = None,
                               alts: list[str] | None = None,
                               verificado: bool | None = None) -> dict | None:
    """
    Convierte las líneas de texto de un bloque de comentario del DOM
    (autor, tiempo, texto, likes, "Responder", ["Ver N respuestas"]) en el
    mismo esquema que usaba la versión por API.

    `comentario_id` es el ID real de Instagram, sacado del permalink
    /p/SHORTCODE/c/<id>/. Cuando está, es también el `comentario_pk` (clave de
    dedup); si el DOM no expuso el permalink, se cae al hash sintético del
    contenido, que fusiona comentarios idénticos del mismo autor y timestamp.

    `posicion_dom` es el orden en que el comentario apareció en el DOM
    (`data-scrp-id`). NO es una posición global de scrapeo: el contador
    vive en `document.body`, o sea que arranca de cero en cada carga de
    página. Por eso viaja junto a `carga_id`, y `posicion_scrapeo` se deriva
    después, offline, agrupando por carga.
    """
    if len(lineas) < 2:
        return None
    autor = lineas[0]
    cuerpo = lineas[1:]  # todo menos el autor

    # La línea de metadata es la que TERMINA en "Responder". Puede venir sola
    # ("Responder", layout del panel del post) o pegada con tiempo y likes
    # ("2 d349 Me gustaResponder", layout de la página de permalink). Se busca de
    # atrás para adelante: es lo último del bloque antes de traducción/respuestas.
    idx_meta = None
    for i in range(len(cuerpo) - 1, -1, -1):
        if cuerpo[i].rstrip().endswith("Responder"):
            idx_meta = i
            break
    if idx_meta is None:
        return None  # bloque sin "Responder": no es un comentario real, se descarta

    linea_meta = cuerpo[idx_meta]
    likes = 0
    fin_texto = idx_meta
    m_inline = _RE_LIKES_INLINE.search(linea_meta)
    if m_inline:
        # layout permalink: los likes están dentro de la línea de metadata.
        likes = int(m_inline.group(1).replace(".", "").replace(",", ""))
    elif idx_meta > 0:
        # layout post: los likes son la línea separada justo antes de "Responder".
        m_sep = _RE_LIKES.match(cuerpo[idx_meta - 1])
        if m_sep:
            likes = int(m_sep.group(1).replace(".", "").replace(",", ""))
            fin_texto = idx_meta - 1

    # reply_count: en cualquiera de sus formas, en las líneas post-metadata.
    reply_count = 0
    for j in range(idx_meta, len(cuerpo)):
        reply_count = _reply_count_de_linea(cuerpo[j])
        if reply_count:
            break

    # El texto es lo que queda antes de la metadata, sacando la línea de tiempo
    # relativo (layout post) y botones de UI ("Ver traducción").
    texto_lineas = cuerpo[:fin_texto]
    if texto_lineas and _RE_RELTIME.match(texto_lineas[0].strip()):
        texto_lineas = texto_lineas[1:]
    texto_lineas = [l for l in texto_lineas if l.strip() != "Ver traducción"]
    texto = "\n".join(texto_lineas).strip()
    m_id = _RE_COMMENT_ID.search(href) if href else None
    comentario_id = m_id.group(1) if m_id else None
    pk_sintetico = hashlib.sha1(
        f"{post['media_id']}|{autor}|{texto}|{tiempo_iso}".encode("utf-8")
    ).hexdigest()[:20]

    return {
        "comentario_pk": comentario_id or pk_sintetico,  # ID real si lo hay, si no el hash
        "comentario_id": comentario_id,      # ID real de IG (None si el DOM no lo dio)
        "media_id": post["media_id"],
        "shortcode": post.get("shortcode"),
        "cuenta_post": post.get("cuenta"),
        "parent_pk": parent_pk,              # None = comentario raíz
        "autor": autor,
        "autor_pk": None,                    # no disponible sin la API
        "autor_verificado": verificado,      # badge leído del DOM
        "texto": texto,
        "tiene_imagen": bool(alts) if alts is not None else None,
        "alt_imagen": alts[0] if alts else None,
        "created_at": tiempo_iso,            # ISO exacto, tomado de <time datetime=...>
        "like_count": likes,
        "reply_count": reply_count,
        # --- procedencia de la extracción (insumo de posicion_scrapeo, P2) ---
        "posicion_dom": posicion_dom,        # orden dentro de ESTA carga de página
        "carga_id": carga_id,                # una carga = una numeración desde 0
        "fase": fase,                        # 1 scroll de raíces | 2 hilos | 3 permalinks
        "origen": origen,                    # panel | permalink
        # --- evidencia de la decisión raíz/respuesta (en el DOM se decide con
        # x > umbral). Guardarla permite auditar después cuántos casos quedaron
        # cerca del límite.
        "x_bloque": x_bloque,                # x del bloque, en px
        "umbral_indentacion": umbral_indentacion,   # min_x + 10 de esa pasada
        "min_x": min_x,                      # x mínima observada en esa pasada
        "scrapeado_en": datetime.now(tz=timezone.utc).isoformat(),
    }


def comentario_de_api(item: dict, post: dict, carga_id: str | None = None,
                      fase: int | None = None,
                      origen: str = "api_child_comments") -> dict | None:
    """
    Traduce un item de `/api/v1/media/<media>/comments/<id>/child_comments/` al
    MISMO esquema que `_parsear_bloque_comentario`, para que el `.jsonl` sea
    homogéneo venga la fila del DOM o de la red.

    De dónde sale ese JSON: la propia página del permalink lo pide cuando se
    clickea "Ver respuestas (N)"; acá no se fabrica ninguna request, se lee la
    respuesta que el navegador ya recibió. Es lo que evita depender del DOM para
    las respuestas, que es donde se rompía todo: `parent_comment_id` viene
    explícito en vez de deducirse de la indentación en píxeles.

    Lo que este camino da y el DOM no:
      - `autor_pk`: el ID numérico de usuario, estable aunque el usuario cambie
        de handle (por DOM solo se ve el handle).
      - `created_at` exacto en epoch, sin depender del <time>.
      - `posicion_dom` = `child_comment_index`, la posición en el orden SERVIDO
        dentro del hilo.

    Lo que NO da: la señal de sticker/imagen, que se leía de los <img alt> del
    bloque. Queda en None (desconocido), no en False.
    """
    pk = item.get("pk") or item.get("id")
    if not pk:
        return None
    usuario = item.get("user") or {}
    ts = item.get("created_at_utc") or item.get("created_at")
    creado = None
    if ts:
        try:
            creado = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z")     # mismo formato que el <time datetime>
        except (TypeError, ValueError, OSError):
            creado = None
    autor_pk = usuario.get("pk") or usuario.get("id") or item.get("user_id")
    padre = item.get("parent_comment_id")
    return {
        "comentario_pk": str(pk),
        "comentario_id": str(pk),
        "media_id": post["media_id"],
        "shortcode": post.get("shortcode"),
        "cuenta_post": post.get("cuenta"),
        "parent_pk": str(padre) if padre else None,
        "autor": usuario.get("username"),
        "autor_pk": str(autor_pk) if autor_pk else None,
        "autor_verificado": usuario.get("is_verified"),
        "texto": item.get("text") or "",
        "tiene_imagen": None,                # el JSON no trae la señal del DOM
        "alt_imagen": None,
        "created_at": creado,
        "like_count": item.get("comment_like_count", 0),
        "reply_count": item.get("child_comment_count", 0) or 0,
        "posicion_dom": item.get("child_comment_index"),
        "carga_id": carga_id,
        "fase": fase,
        "origen": origen,
        # La indentación no interviene: el padre viene dado. Los tres campos se
        # mantienen (en None) para no romper el esquema de las otras filas.
        "x_bloque": None,
        "umbral_indentacion": None,
        "min_x": None,
        "scrapeado_en": datetime.now(tz=timezone.utc).isoformat(),
    }


def _extraer_del_dom(page, post: dict, vistos: set, nuevos: list, id_a_pk: dict,
                     max_com: int | None, objetivo: int | None,
                     flush=None, carga_id: str | None = None,
                     fase: int | None = None, origen: str = "panel") -> tuple[int, bool]:
    """
    Vuelca al listado los bloques del DOM que todavía no se procesaron.
    Devuelve (cuántos nuevos, si se alcanzó el tope de max_comentarios).
    `flush` (opcional) baja a disco lo acumulado hasta el momento.

    `carga_id` identifica la carga de página en curso: el contador de posición
    del DOM se reinicia con cada navegación, así que sin él dos cargas del
    mismo post producen dos numeraciones desde 0 y no se pueden ordenar juntas.

    `origen` decide además DE DÓNDE se leen los bloques: en el permalink el
    panel scrolleable no contiene el hilo, así que hay que leer el documento
    entero (ver _JS_EXTRAER_COMENTARIOS).
    """
    n = 0
    raiz_dom = "documento" if origen == "permalink" else "panel"
    for crudo in page.evaluate(_JS_EXTRAER_COMENTARIOS, raiz_dom):
        padre_pk = id_a_pk.get(crudo.get("padre_id")) if crudo.get("padre_id") else None
        try:
            posicion_dom = int(crudo["id"])
        except (TypeError, ValueError):
            posicion_dom = None
        parsed = _parsear_bloque_comentario(
            crudo["lineas"], crudo.get("tiempo_iso"), post, parent_pk=padre_pk,
            href=crudo.get("href"),
            posicion_dom=posicion_dom, carga_id=carga_id, fase=fase, origen=origen,
            x_bloque=crudo.get("x"), umbral_indentacion=crudo.get("umbral"),
            min_x=crudo.get("min_x"),
            alts=crudo.get("alts"), verificado=crudo.get("verificado"),
        )
        if not parsed:
            continue
        id_a_pk[crudo["id"]] = parsed["comentario_pk"]
        if parsed["comentario_pk"] in vistos:
            continue
        vistos.add(parsed["comentario_pk"])
        nuevos.append(parsed)
        n += 1
        # Progreso: un post grande puede llevar horas y el resumen recién se
        # escribe al final. Sin esto no hay forma de distinguir "avanzando
        # lento" de "colgado".
        if len(nuevos) % 250 == 0:
            pct = f" ({len(nuevos)/objetivo:.0%} de {objetivo})" if objetivo else ""
            log.info(f"    ... {len(nuevos)} comentarios{pct}")
            # Checkpoint: un post grande puede tardar horas y morir a mitad
            # (OOM del renderer, corte manual). Sin esto se perdía TODO lo
            # bajado; con esto el post se reprocesa y el dedup por pk evita
            # duplicar lo ya escrito.
            if flush:
                flush()
        if max_com and len(nuevos) >= max_com:
            return n, True
    return n, False


def _detectar_bloqueo_url(page):
    url = page.url
    if "/accounts/login" in url or "/challenge/" in url:
        raise BloqueoInstagram(
            f"El navegador fue redirigido a {url} — sesión inválida o challenge. "
            f"NO reintentar en caliente."
        )


def construir_url_post(post: dict) -> str:
    # ?hl=es-la fuerza el idioma de la UI a español: el parser busca literales
    # como "Responder"/"Me gusta". Sin esto, Instagram puede servir la página
    # en inglés (depende del locale del navegador/cuenta) y no matchea nada.
    return f"https://www.instagram.com/p/{post['shortcode']}/?hl=es-la"


def estado_inicial(post: dict) -> dict:
    """
    Fila de `estado.csv` con todas sus columnas ya declaradas.

    Es la ÚNICA definición del esquema de estado.csv: la usan tanto la corrida
    normal como el camino de error de main(). Antes el camino de error armaba
    su propio dict a mano y era fácil que quedaran desincronizados; ahora
    append_csv (ver A1) cortaría la corrida si eso pasara.
    """
    return {
        "media_id": post["media_id"],
        "shortcode": post.get("shortcode"),
        "cuenta": post.get("cuenta"),
        "url": None,
        "carga_id": None,
        "post_like_count": None,
        "post_comment_count": None,
        "post_meta_crudo": None,
        # En modo post_urls caption, taken_at y edad_post_horas solo se
        # consiguen acá, del navegador.
        "caption": post.get("caption"),
        "taken_at": post.get("taken_at"),
        "edad_post_horas": None,          # captura − publicación
        "comment_count_reportado": post.get("comment_count"),
        "n_extraidos": 0,
        "n_raiz": 0,
        "n_respuestas": 0,
        "cobertura": None,        # n_extraidos / post_comment_count
        "estado": None,
        # Esfuerzo y duración de la corrida sobre este post (reloj de pared,
        # UTC). Sirve para presupuestar corridas futuras.
        "t_inicio": None,
        "t_fin": None,
        "duracion_seg": None,
        "iteraciones": None,
        "pasadas_fase2": None,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


def scrape_comentarios_post_navegador(context, post: dict, cfg: dict, vistos: set,
                                      persistir=None) -> tuple[list[dict], dict]:
    """
    Abre el post en el navegador, scrollea el contenedor de comentarios y va
    extrayendo cada comentario nuevo del DOM. Devuelve (comentarios_nuevos, estado).

    `persistir(lote)` se llama periódicamente con los comentarios todavía no
    escritos a disco: el llamador no debe volver a guardarlos al final.
    """
    estado = estado_inicial(post)

    max_com = cfg["max_comentarios_por_post"]
    # Cortamos solo tras N rondas en las que NO entró ningún comentario nuevo Y
    # ADEMÁS el panel no avanzó. Chequear solo lo primero cortaba de más: si IG
    # tarda en servir el lote siguiente, unas pocas rondas vacías seguidas
    # hacían terminar la corrida con "ok_completo" habiendo bajado una fracción
    # de los comentarios (medido: 1753 de 8679). Falso completo > error visible.
    max_sin_novedad = 8
    # Tope de seguridad ante scroll infinito. Es un piso: cuando se sepa
    # cuántos comentarios declara el post, se escala con él (ver más abajo).
    # Honestamente: ninguna corrida llegó nunca a este tope — siempre cortó
    # antes por `sin_novedad` o por el límite blando de Instagram. Es higiene,
    # no un arreglo de algo que estuviera fallando.
    MIN_ITERACIONES = 5000
    max_iteraciones = MIN_ITERACIONES
    max_minutos = cfg.get("max_minutos_por_post")
    t_inicio = time.monotonic()
    estado["t_inicio"] = datetime.now(tz=timezone.utc).isoformat()
    iteraciones = 0
    pasadas_fase2 = 0
    nuevos: list[dict] = []
    persistidos = 0

    def flush():
        nonlocal persistidos
        if persistir and len(nuevos) > persistidos:
            persistir(nuevos[persistidos:])
            persistidos = len(nuevos)

    # Una carga de página = una numeración de posición del DOM desde 0. Si el
    # post se retoma tras un corte, la segunda corrida es OTRA carga y su
    # posicion_dom vuelve a empezar: sin este id las dos series se mezclarían.
    carga_id = uuid.uuid4().hex[:8]
    estado["carga_id"] = carga_id

    page = context.new_page()
    try:
        page.goto(construir_url_post(post), wait_until="networkidle", timeout=30000)
        _detectar_bloqueo_url(page)
        page.wait_for_timeout(1500)
        # Localizar el panel de comentarios una sola vez: de acá en adelante
        # extracción, expansión y scroll trabajan sobre ese subárbol.
        init = page.evaluate(_JS_INIT_CONTENEDOR)
        if not init.get("ok"):
            log.info("  (sin panel de comentarios scrolleable; el post debe tener pocos)")
        estado.update(extraer_meta_post(page, post))
        if estado["comment_count_reportado"] is None:
            estado["comment_count_reportado"] = estado["post_comment_count"]
        # En modo post_urls la cuenta se descubre recién acá (de og:url): la
        # propagamos al post para que cada comentario lleve su `cuenta_post`.
        post["cuenta"] = estado["cuenta"]
        # Antes de scrollear: el viewport todavía muestra el encabezado del post.
        # No se guarda en `estado`: la ruta es data/capturas/<shortcode>.png, o
        # sea derivable, y una columna nueva obligaría a rotar estado.csv en
        # plena recolección (ver append_csv).
        guardar_captura_post(page, post)

        objetivo = estado["post_comment_count"]   # cuántos comentarios dice IG que hay
        id_a_pk: dict[str, str] = {}              # data-scrp-id -> comentario_pk (para el padre)

        # Un post de 8.000 comentarios necesita legítimamente más pasadas que
        # uno de 200, así que el tope escala con el tamaño declarado. El factor
        # 2 es holgado a propósito: esto tiene que atrapar un loop infinito, no
        # cortar una corrida lenta pero sana.
        if objetivo:
            max_iteraciones = max(MIN_ITERACIONES, objetivo * 2)

        def limite_alcanzado() -> str | None:
            if iteraciones > max_iteraciones:
                log.warning("  ⚠ Corte por límite de iteraciones (posible loop infinito)")
                return "ok_corte_seguridad"
            if max_minutos and (time.monotonic() - t_inicio) / 60 >= max_minutos:
                log.warning(f"  ⚠ Corte por límite de tiempo ({max_minutos} min); "
                            f"el post queda parcial (ver cobertura)")
                return "ok_corte_tiempo"
            return None

        # --- FASE 1: cargar TODOS los comentarios raíz scrolleando ----------
        # Los hilos se despliegan después, en la fase 2, y no acá: al expandir
        # un hilo el final del panel pasa a ser una respuesta en vez del último
        # comentario raíz, y entonces Instagram deja de cargar más comentarios.
        # Medido: mezclando ambas cosas el mismo post cortaba en 124 en vez de
        # traer 283.
        sin_novedad = 0
        while True:
            n_new, tope = _extraer_del_dom(page, post, vistos, nuevos, id_a_pk, max_com,
                                           objetivo, flush,
                                           carga_id=carga_id, fase=1, origen="panel")
            if tope:
                estado["estado"] = "ok_tope"
                break

            iteraciones += 1
            corte = limite_alcanzado()
            if corte:
                estado["estado"] = corte
                break

            res = page.evaluate(_JS_SCROLL_CONTENEDOR)
            if not res.get("ok"):
                # Sin panel scrolleable: el post tiene pocos comentarios y
                # entran todos, o cambió el layout de Instagram.
                break

            if n_new or res.get("avanzo"):
                sin_novedad = 0
            else:
                sin_novedad += 1
                # Paciencia adaptativa: si todavía estamos LEJOS del total que
                # declara Instagram, insistir mucho más antes de dar el post por
                # terminado. Rendirse rápido con cobertura baja es justo lo que
                # producía los falsos "ok_completo".
                paciencia = max_sin_novedad
                if objetivo:
                    cobertura = len(nuevos) / objetivo
                    if cobertura < 0.25:
                        paciencia = max_sin_novedad * 4
                    elif cobertura < 0.60:
                        paciencia = max_sin_novedad * 2
                if sin_novedad >= paciencia:
                    break
                # Backoff creciente: puede ser que IG esté tardando, no que
                # se hayan terminado los comentarios.
                page.wait_for_timeout(min(2000 * sin_novedad, 20000))

            delay(cfg["delay_entre_requests"])

        # --- FASE 2: desplegar los hilos y recolectar las respuestas --------
        # Ya no hace falta scrollear: con todos los raíz en el DOM, los botones
        # "Ver las N respuestas" se encuentran y se clickean estén o no en
        # pantalla.
        if cfg["incluir_respuestas"] and estado["estado"] is None:
            log.info(f"    fase 2: desplegando hilos ({len(nuevos)} raíz cargados)")
            sin_novedad = 0
            while True:
                t_ext = time.monotonic()
                n_new, tope = _extraer_del_dom(page, post, vistos, nuevos, id_a_pk, max_com,
                                           objetivo, flush,
                                           carga_id=carga_id, fase=2, origen="panel")
                dt_ext = time.monotonic() - t_ext
                pasadas_fase2 += 1
                if tope:
                    estado["estado"] = "ok_tope"
                    break

                iteraciones += 1
                corte = limite_alcanzado()
                if corte:
                    estado["estado"] = corte
                    break

                # Clicks por pasada: con 5 (el valor original) un post con
                # cientos de hilos necesitaba cientos de pasadas, y cada pasada
                # paga el escaneo del panel entero + el delay. 30 mantiene el
                # ritmo de requests razonable y baja las pasadas un orden de
                # magnitud.
                t_exp = time.monotonic()
                exp = page.evaluate(_JS_EXPANDIR_RESPUESTAS, {"limite": 30, "modo": "panel"})
                dt_exp = time.monotonic() - t_exp
                # Una línea por pasada: sin esto la fase 2 es una caja negra y no
                # hay forma de distinguir "el DOM está lento" de "Instagram dejó
                # de servir respuestas" de "no quedan hilos por abrir".
                log.info(f"      pasada: +{n_new} com | clicks={exp.get('clicks')} "
                         f"(hilos nuevos={exp.get('ver')}) "
                         f"pendientes={exp.get('pendientes')} "
                         f"agotados={exp.get('agotados')} | "
                         f"extraer {dt_ext:.1f}s expandir {dt_exp:.1f}s | "
                         f"sin_novedad={sin_novedad}")
                # El ÚNICO progreso válido es que haya entrado un comentario
                # nuevo. Ni `clicks` ni `ver` sirven: los paginadores se
                # re-clickean por diseño (ver _JS_EXPANDIR_RESPUESTAS) y usarlos
                # como señal de vida deja el loop girando para siempre contra un
                # Instagram que ya no responde (medido: 2,5 h el 22/7).
                if n_new:
                    sin_novedad = 0
                else:
                    sin_novedad += 1
                    # Nada nuevo Y nada que clickear: no hay a qué esperarle.
                    if not exp.get("clicks") and not exp.get("pendientes"):
                        break
                    if sin_novedad >= max_sin_novedad:
                        break
                    page.wait_for_timeout(min(1500 * sin_novedad, 10000))

                delay(cfg["delay_entre_requests"])
    finally:
        # Incluso si el post se corta por excepción, lo bajado queda en disco.
        flush()
        page.close()

    estado["n_extraidos"] = len(nuevos)
    estado["n_respuestas"] = sum(1 for c in nuevos if c["parent_pk"])
    estado["n_raiz"] = len(nuevos) - estado["n_respuestas"]
    estado["t_fin"] = datetime.now(tz=timezone.utc).isoformat()
    estado["duracion_seg"] = round(time.monotonic() - t_inicio, 1)
    estado["iteraciones"] = iteraciones
    estado["pasadas_fase2"] = pasadas_fase2
    if estado["estado"] is None:
        estado["estado"] = "ok_completo"
    total = estado["post_comment_count"]
    if total:
        estado["cobertura"] = round(len(nuevos) / total, 3)
        # Instagram corta la paginación devolviendo `has_next_page: false` con
        # lista vacía, sin error ni challenge: un "no hay más" limpio aunque el
        # post declare muchos más comentarios. Pasa cuando la cuenta viene
        # scrapeando fuerte hace rato. Distinguirlo de una corrida realmente
        # completa es imprescindible para no reportar una muestra truncada
        # como si fuera el universo.
        if estado["estado"] == "ok_completo" and estado["cobertura"] < 0.5:
            estado["estado"] = "ok_truncado_por_ig"
            log.warning(f"  ⚠ Instagram cortó la paginación en {len(nuevos)} de {total} "
                        f"({estado['cobertura']:.0%}). Suele ser límite blando por scrapear "
                        f"seguido: pará unas horas y retomá.")
    return nuevos, estado

# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def append_jsonl(path: Path, filas: list[dict]):
    if not filas:
        return
    with path.open("a", encoding="utf-8") as f:
        for fila in filas:
            f.write(json.dumps(fila, ensure_ascii=False) + "\n")


def append_csv(path: Path, filas: list[dict]):
    """
    Append a un CSV, verificando que las claves de la fila coincidan con el
    header ya escrito.

    Sin esta verificación, agregar una columna al esquema hace que las filas
    nuevas se escriban con otro conjunto de campos bajo el header viejo: el
    archivo queda desalineado y csv.DictWriter no se queja. Es corrupción
    silenciosa, y aparece justo cuando el esquema evoluciona (que es siempre).
    Ante desajuste se corta: el arreglo es rotar el archivo, no seguir
    escribiendo.
    """
    if not filas:
        return
    campos = list(filas[0].keys())
    escribir_header = True
    if path.exists() and path.stat().st_size > 0:
        with path.open(encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), None)
        if header is not None:
            escribir_header = False
            if header != campos:
                faltan = [c for c in header if c not in campos]
                sobran = [c for c in campos if c not in header]
                raise RuntimeError(
                    f"El header de {path.name} no coincide con las columnas a escribir. "
                    f"En el archivo y no en la fila: {faltan or '-'}. "
                    f"En la fila y no en el archivo: {sobran or '-'}. "
                    f"Rotá el archivo (renombralo a {path.stem}_v1{path.suffix}) y volvé "
                    f"a correr; no se escribió nada."
                )
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        if escribir_header:
            w.writeheader()
        w.writerows(filas)


def cargar_pks_vistos() -> set:
    """Dedup: pks de comentarios ya guardados, para no duplicar al retomar."""
    vistos = set()
    if COMENTARIOS_FILE.exists():
        with COMENTARIOS_FILE.open(encoding="utf-8") as f:
            for linea in f:
                try:
                    vistos.add(json.loads(linea)["comentario_pk"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return vistos


def contar_raices_por_media() -> dict:
    """
    Cuántos comentarios RAÍZ hay ya guardados de cada post, contados del
    `.jsonl`, que es la única fuente que habla del POST y no de la corrida.

    `estado.csv` es un log append-only por corrida: un post bajado en dos
    tandas deja dos filas de 250 y ninguna dice 500. El tope de raíces por
    post es una cantidad del post, así que compararlo contra una
    fila de estado.csv da siempre menos de lo que hay y el post se reabre para
    siempre. Acá se agrupa por media_id y el número es el de verdad.

    Se cuentan solo las raíces (`parent_pk` vacío) porque el tope es de raíces:
    las respuestas las baja la pasada 2 al mismo archivo y no deben consumir
    presupuesto de la pasada 1.
    """
    conteos = {}
    if not COMENTARIOS_FILE.exists():
        return conteos
    with COMENTARIOS_FILE.open(encoding="utf-8") as f:
        for linea in f:
            try:
                c = json.loads(linea)
            except json.JSONDecodeError:
                continue
            if c.get("parent_pk"):
                continue
            mid = c.get("media_id")
            if mid:
                conteos[mid] = conteos.get(mid, 0) + 1
    return conteos


def cargar_media_ids_resueltos(tope: int | None = None) -> set:
    """
    media_ids de posts ya procesados, para el resume.

    Un post cuenta como resuelto por dos vías:

    1. `estado.csv` lo dejó en ESTADOS_RESUELTOS. Antes se miraba
       `estado.startswith("ok")`, pero `ok_truncado_por_ig`, `ok_corte_tiempo`,
       `ok_corte_seguridad` y `ok_tope` también empiezan con "ok": un post
       cortado al 20% quedaba marcado como listo y no se reintentaba nunca.
    2. En el `.jsonl` ya hay `tope` raíces suyas o más. Esto es lo que hace que
       el flujo funcione con las URLs acumuladas en config.json: la pregunta
       "¿este enlace ya está?" se contesta contando los datos, no confiando en
       que alguna corrida haya alcanzado el tope de una sola sentada.

    Con el flujo de ir agregando URLs a config.json y volver a correr, el
    resume es lo único que evita re-scrapear lo ya hecho, así que un parcial
    mal clasificado se perdería para siempre.
    """
    resueltos = set()
    if ESTADO_FILE.exists():
        with ESTADO_FILE.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("estado") or "").strip() in ESTADOS_RESUELTOS:
                    resueltos.add(row["media_id"])
    if tope:
        for mid, n in contar_raices_por_media().items():
            if n >= tope:
                resueltos.add(mid)
    return resueltos


def informe_estado(cfg: dict) -> None:
    """
    Imprime, por post, cuántas raíces hay bajadas y si cuenta como resuelto.
    Es la respuesta a "¿este enlace ya está?" sin abrir el navegador: no toca
    Instagram, solo lee `comentarios.jsonl`, `estado.csv` y config.json.

    Existe porque ninguna de las dos fuentes contesta sola. `estado.csv` habla
    de la CORRIDA (un post bajado en dos tandas deja dos filas y ninguna dice
    el total) y el `.jsonl` no sabe cuántos comentarios declaraba el post. El
    conteo de raíces sale del `.jsonl`, el declarado de `estado.csv`.
    """
    tope = cfg["max_comentarios_por_post"]
    raices = contar_raices_por_media()

    posts = {}
    if ESTADO_FILE.exists():
        with ESTADO_FILE.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                d = posts.setdefault(row["media_id"],
                                     {"shortcode": None, "declarado": None, "estados": []})
                d["shortcode"] = row.get("shortcode") or d["shortcode"]
                try:
                    declarado = int(row.get("post_comment_count") or 0)
                except ValueError:
                    declarado = 0
                if declarado:
                    d["declarado"] = declarado
                if (row.get("estado") or "").strip():
                    d["estados"].append(row["estado"].strip())

    # Las URLs de config que todavía no se tocaron no están en ninguna de las
    # dos fuentes, y son justamente las que interesa ver como pendientes.
    for url in cfg["post_urls"]:
        sc = shortcode_de_url(url)
        if sc:
            posts.setdefault(str(shortcode_a_media_id(sc)),
                             {"shortcode": sc, "declarado": None, "estados": []})
    for mid in raices:
        posts.setdefault(mid, {"shortcode": None, "declarado": None, "estados": []})

    resueltos = cargar_media_ids_resueltos(tope)

    log.info(f"Tope de raíces por post (config): {tope if tope else 'sin tope'}")
    log.info(f"{'shortcode':<14} {'raíces':>7} {'declar.':>8} {'frac':>6}  {'':<5} última corrida")
    log.info("-" * 78)
    n_listos = 0
    for mid, d in sorted(posts.items(), key=lambda kv: kv[1]["shortcode"] or ""):
        n = raices.get(mid, 0)
        listo = mid in resueltos
        n_listos += listo
        frac = f"{n / d['declarado']:.2f}" if d["declarado"] else "—"
        log.info(
            f"{d['shortcode'] or mid:<14} {n:>7} {d['declarado'] or '—':>8} {frac:>6}  "
            f"{'LISTO' if listo else '·':<5} {d['estados'][-1] if d['estados'] else 'nunca bajado'}"
        )
    log.info("-" * 78)
    log.info(f"{len(posts)} posts: {n_listos} resueltos, {len(posts) - n_listos} pendientes")
    log.info("`frac` es sobre lo DECLARADO por IG, que incluye respuestas: con tope "
             "fijo no es un indicador de completitud.")


def guardar_cola(cola: list[dict]):
    COLA_FILE.write_text(json.dumps(cola, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------------------------------------------------------------------
# Construcción de la cola de trabajo
# ---------------------------------------------------------------------------

def construir_cola(sesion, cfg: dict) -> list[dict]:
    """
    Arma la lista de posts a scrapear: lo que quedó pendiente de corridas
    anteriores MÁS lo que hay en config.json, sin duplicar.

    ⚠ Si con cola pendiente se devolviera solo esa cola sin leer config.json,
    las URLs agregadas después de un bloqueo (que es justo cuando la cola queda
    con pendientes) no entrarían nunca, sin ningún error. El modo de fallar
    tiene que ser "se scrapea de más", nunca "se saltea sin avisar".
    """
    pendientes = []
    if COLA_FILE.exists():
        try:
            pendientes = json.loads(COLA_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning(f"{COLA_FILE.name} ilegible; se reconstruye desde config.json")
    if pendientes:
        log.info(f"Cola persistida: {len(pendientes)} posts pendientes")

    cola = list(pendientes)
    conocidos = {p["media_id"] for p in cola}

    # El descubrimiento por cuenta pega contra Instagram; no se repite mientras
    # haya cola pendiente, porque esos posts ya están en ella. Resolver un
    # post_url, en cambio, es aritmética local (shortcode → media_id, sin
    # request), así que eso sí se hace siempre: es lo que garantiza que una URL
    # nueva entre aunque la cola venga arrastrando pendientes.
    if cfg["cuentas"] and pendientes:
        log.info("Descubrimiento por cuenta salteado: hay cola pendiente (no se gastan requests)")
    else:
        for username in cfg["cuentas"]:
            log.info(f"Descubriendo posts de @{username} ...")
            posts = descubrir_posts(sesion, username, cfg)
            log.info(f"  {len(posts)} posts")
            for p in posts:
                if p["media_id"] not in conocidos:
                    cola.append(p)
                    conocidos.add(p["media_id"])
            append_csv(POSTS_FILE, posts)
            delay(cfg["delay_entre_posts"])

    n_config = 0
    for url in cfg["post_urls"]:
        sc = shortcode_de_url(url)
        if not sc:
            log.warning(f"  URL no reconocida, se salta: {url}")
            continue
        media_id = str(shortcode_a_media_id(sc))
        if media_id in conocidos:
            continue
        cola.append({
            "cuenta": None,
            "shortcode": sc,
            "media_id": media_id,
            "caption": None,
            "comment_count": None,
            "taken_at": None,
        })
        conocidos.add(media_id)
        n_config += 1
    if n_config:
        log.info(f"Agregados desde config.json: {n_config} posts nuevos")

    # Excluir los ya resueltos: por estado.csv, o porque el .jsonl ya tiene el
    # tope de raíces de ese post. Esto es lo que permite dejar las URLs
    # acumuladas en config.json para siempre sin re-scrapear nada.
    resueltos = cargar_media_ids_resueltos(cfg["max_comentarios_por_post"])
    if resueltos:
        antes = len(cola)
        cola = [p for p in cola if p["media_id"] not in resueltos]
        if antes != len(cola):
            log.info(f"Resume: {antes - len(cola)} posts ya resueltos se saltean")

    guardar_cola(cola)
    return cola

# ---------------------------------------------------------------------------
# Navegador
# ---------------------------------------------------------------------------

def crear_contexto_navegador(browser, cookies: dict):
    """Contexto de Playwright con la cookie de sesión ya inyectada."""
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=USER_AGENT,
    )
    context.add_cookies([
        {"name": k, "value": v, "domain": ".instagram.com", "path": "/"}
        for k, v in cookies.items() if v
    ])
    return context

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Scraper de comentarios de Instagram")
    ap.add_argument("--reset", action="store_true",
                    help="Ignora la cola persistida y la reconstruye desde config.json")
    ap.add_argument("--estado", action="store_true",
                    help="Muestra qué posts ya están resueltos y cuáles faltan, y termina. "
                         "No abre el navegador ni toca Instagram")
    ap.add_argument("--config", default=str(CONFIG_FILE),
                    help="Archivo de configuración (default: config.json)")
    ap.add_argument("--salida", default=None,
                    help="Carpeta de salida (default: data/). Útil para separar proyectos")
    ap.add_argument("--headful", action="store_true",
                    help="Muestra la ventana del navegador (para debuggear)")
    args = ap.parse_args()

    configurar_rutas(args.salida)
    configurar_logging("ig")

    log.info("=" * 60)
    log.info("Scraper de comentarios de Instagram v2 (extracción por navegador)")
    log.info("=" * 60)

    cfg = cargar_config(args.config)
    if not cfg["cuentas"] and not cfg["post_urls"]:
        log.error("config.json no tiene ni 'cuentas' ni 'post_urls'. Nada que hacer.")
        return

    if args.estado:
        informe_estado(cfg)
        return

    cookies = cargar_sesion()
    sesion = crear_sesion_http(cookies)  # sigue usándose para descubrir posts (feed/user)

    if cfg["incluir_respuestas"]:
        log.warning("  incluir_respuestas: ON — además de lento, PISA a pasada_respuestas.py: "
                    "las respuestas quedan en calidad DOM (sin autor_pk, padre por indentación) "
                    "y el JSON de la API se descarta por dedup. Para hilos, mejor false.")

    if args.reset and COLA_FILE.exists():
        COLA_FILE.unlink()
        log.info("Cola reiniciada (--reset)")

    vistos = cargar_pks_vistos()
    log.info(f"Comentarios ya guardados (dedup): {len(vistos)}")

    try:
        cola = construir_cola(sesion, cfg)
    except BloqueoInstagram as e:
        log.error(f"Bloqueo durante el descubrimiento: {e}")
        return

    log.info(f"Posts a procesar: {len(cola)}")

    n_ok = n_error = n_comentarios = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        context = crear_contexto_navegador(browser, cookies)

        try:
            while cola:
                post = cola[0]
                etiqueta = post.get("shortcode") or post["media_id"]
                log.info(f"\n[{len(cola)} en cola] post {etiqueta} (@{post.get('cuenta')})")

                try:
                    comentarios, estado = scrape_comentarios_post_navegador(
                        context, post, cfg, vistos,
                        persistir=lambda lote: append_jsonl(COMENTARIOS_FILE, lote),
                    )
                except BloqueoInstagram as e:
                    # Bloqueo = retirarse. La cola queda intacta (este post sin resolver)
                    # para retomar más tarde. Insistir solo empeora el bloqueo.
                    log.error(f"⚠ BLOQUEO: {e}")
                    log.error("Cortando la corrida. La cola quedó persistida; retomá en unas horas.")
                    guardar_cola(cola)
                    break
                except Exception as e:
                    log.error(f"  Error inesperado en {etiqueta}: {e}")
                    fila = estado_inicial(post)
                    fila["estado"] = "error_inesperado"
                    append_csv(ESTADO_FILE, [fila])
                    cola.pop(0)
                    guardar_cola(cola)
                    n_error += 1
                    continue

                # Los comentarios ya se fueron escribiendo durante el post (ver
                # `persistir`), así que acá solo va el estado y recién ahí sale
                # el post de la cola. Si crashea antes, el post se reprocesa (el
                # dedup por pk evita duplicar los comentarios ya escritos).
                append_csv(ESTADO_FILE, [estado])
                cola.pop(0)
                guardar_cola(cola)

                n_ok += 1
                n_comentarios += len(comentarios)
                log.info(f"  → {len(comentarios)} comentarios nuevos ({estado['estado']})")

                if cola:
                    delay(cfg["delay_entre_posts"])
        finally:
            browser.close()

    # --- Resumen ---
    log.info("\n--- RESUMEN (esta corrida) ---")
    log.info(f"Posts procesados OK: {n_ok}")
    log.info(f"Posts con error:     {n_error}")
    log.info(f"Comentarios nuevos:  {n_comentarios}")
    log.info(f"Pendientes en cola:  {len(cola)}")
    log.info(f"Output:  {COMENTARIOS_FILE}")
    log.info(f"Estado:  {ESTADO_FILE}")
    if not cola:
        log.info("Cola vacía = corrida completa.")


if __name__ == "__main__":
    main()
