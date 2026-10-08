#!/usr/bin/env python3
"""
Pasada 2: respuestas de los hilos.

Proceso SEPARADO de la pasada 1 (scraper_comentarios_ig.py). La pasada 1 guarda
el `comentario_id` real de cada comentario raíz (sacado del permalink
/p/SHORTCODE/c/<id>/). Esta pasada lee esos raíz y, para cada uno con
`reply_count > 0`, baja sus respuestas al MISMO `comentarios.jsonl`, con
`parent_pk` = id real del raíz.

Por qué separado y no desplegando hilos dentro del panel del post:
  - desplegar hilos en el panel exige tener los miles de raíz cargados en un
    solo DOM (llegó a 5+ GB) y no es reintentable por hilo;
  - acá cada hilo se abre por su permalink en una página liviana que se cierra
    enseguida: el DOM nunca se acumula y un hilo que falla se reintenta solo.

Cómo obtiene las respuestas: abre el permalink, clickea "Ver respuestas (N)"
—que es el paginador y va descontando de a 3— y toma las respuestas del JSON
que la propia página pide a /api/v1/.../child_comments/. No se fabrica ninguna
request ni se lee el DOM: el padre (`parent_comment_id`) y el ID numérico del
autor vienen explícitos.

Por defecto se bajan TODOS los hilos. Con `--top-hilos N` se toman solo los N
con más respuestas declaradas de cada post (útil para muestrear posts grandes:
un post con cientos de hilos puede llevar horas de clicks).

Uso:
    python pasada_respuestas.py                 # todos los media_id del jsonl
    python pasada_respuestas.py --media 12345   # solo ese post
    python pasada_respuestas.py --top-hilos 2   # solo los 2 hilos más grandes por post
    python pasada_respuestas.py --reset         # reprocesa hilos ya hechos
    python pasada_respuestas.py --salida datos_otro/   # misma carpeta que la pasada 1
"""
import argparse
import json
import uuid

from playwright.sync_api import sync_playwright

import scraper_comentarios_ig as s

log = s.log

# Por defecto, todos los hilos. Ver --top-hilos.
TOP_HILOS_POR_POST = 0

# Techo de seguridad por hilo. A 3 respuestas por click, 120 pasadas son ~360
# respuestas. Existe para que un botón que Instagram deja de responder no se
# lleve la sesión entera.
MAX_PASADAS_POR_HILO = 120


def hechos_file():
    # Estado propio de la pasada 2: ids reales de los raíz cuyos hilos ya se
    # bajaron. Separado de estado.csv (que audita la pasada 1) para poder
    # reintentar hilos sin tocar lo otro. Es función y no constante porque la
    # carpeta de salida se fija recién en main().
    return s.DATA_DIR / "respuestas_hechas.json"


# ---------------------------------------------------------------------------
# Respuestas de UN hilo, por su permalink
# ---------------------------------------------------------------------------

def respuestas_por_permalink(context, post: dict, raiz: dict, cfg: dict) -> list[dict]:
    """Dado el raíz de un hilo, devuelve sus respuestas (mismo esquema que un
    comentario, con parent_pk = id real del raíz)."""
    url = f"https://www.instagram.com/p/{raiz['shortcode']}/c/{raiz['comentario_id']}/?hl=es-la"
    # Cada hilo es su propia carga de página, con su propia numeración de
    # posicion_dom desde 0. Un carga_id por hilo, entonces.
    carga_id = uuid.uuid4().hex[:8]
    objetivo = raiz.get("comentario_id")

    # Las respuestas se toman de la RED, no del DOM.
    #
    # Al clickear "Ver respuestas (N)" la página pide
    # /api/v1/media/<media>/comments/<raiz>/child_comments/?min_id=...
    # y recibe el hilo en JSON, con `parent_comment_id` explícito. Leer esa
    # respuesta es estrictamente mejor que leer el DOM:
    #   - el padre es un dato, no una inferencia por indentación en píxeles;
    #   - no importa que React re-renderice la lista y se lleve puestas las
    #     marcas `data-scrp` (que es lo que hacía que la lectura incremental
    #     del DOM no viera NUNCA una respuesta nueva, medido 2026-07-28);
    #   - trae `user.pk`, el ID numérico de usuario que el DOM no expone.
    # Los clicks siguen siendo necesarios: son los que disparan la request.
    recibidas: dict = {}          # pk -> item crudo del JSON
    declaradas = {"n": None}      # child_comment_count que reporta IG
    ajenas: set = set()           # otros hilos que la página fue a buscar

    def on_response(resp):
        if "/child_comments/" not in resp.url or resp.status != 200:
            return
        try:
            datos = resp.json()
        except Exception:
            return
        nuestras = 0
        for it in datos.get("child_comments") or []:
            padre = str(it.get("parent_comment_id") or "")
            if padre != objetivo:
                # La página abre también los hilos de otros comentarios del
                # post. NO se guardan: quedarían incompletos, y la selección
                # de qué hilos entran la hace seleccionar_top_hilos().
                ajenas.add(padre)
                continue
            if it.get("pk"):
                if not recibidas:
                    # Una sola vez por corrida: qué campos trae realmente el
                    # JSON. Es lo que permite escribir el mapeo y sus
                    # pruebas contra la forma REAL y no contra una inventada.
                    log.info(f"    campos del item: {sorted(it.keys())}")
                    log.info(f"    campos de user: {sorted((it.get('user') or {}).keys())}")
                recibidas[str(it["pk"])] = it
                nuestras += 1
        # El total declarado se toma SOLO de una respuesta que traiga
        # respuestas de nuestro hilo: si no, el contador de un hilo ajeno se
        # copiaría como si fuera el nuestro y el corte por completitud
        # dispararía cuando no corresponde.
        if nuestras and isinstance(datos.get("child_comment_count"), int):
            declaradas["n"] = datos["child_comment_count"]

    page = context.new_page()
    page.on("response", on_response)
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        s._detectar_bloqueo_url(page)     # login/challenge -> BloqueoInstagram
        page.wait_for_timeout(2500)

        # "Ver respuestas (N)" es el paginador: entrega de a 3 y va
        # descontando, así que hay que clickear hasta agotarlo (~8 clicks
        # para un hilo de 24). Se clickea de a UNO: el primer botón de la
        # página es el del comentario enfocado, y clickear los 9 que hay
        # dispara requests de hilos que no vamos a guardar.
        sin_novedad = 0
        max_sin_novedad = 4
        pasadas = 0
        limite = 1
        ancla = objetivo
        while True:
            antes = len(recibidas)
            exp = page.evaluate(s._JS_EXPANDIR_RESPUESTAS,
                                {"limite": limite, "modo": "documento", "ancla": ancla})
            page.wait_for_timeout(1500)   # la request tarda ~1-4 s en volver
            pasadas += 1
            nuevas = len(recibidas) - antes
            # Una línea por pasada: sin esto el hilo es una caja negra y no
            # se distingue "IG dejó de servir" de "no queda nada que abrir".
            log.info(f"    pasada {pasadas}: +{nuevas} resp "
                     f"({len(recibidas)}/{declaradas['n']}) | "
                     f"clicks={exp.get('clicks')} pendientes={exp.get('pendientes')} "
                     f"agotados={exp.get('agotados')} | sin_novedad={sin_novedad}")
            # El hilo está completo según el propio contador de Instagram.
            if declaradas["n"] is not None and len(recibidas) >= declaradas["n"]:
                break
            # El botón del hilo ya no está: Instagram terminó de servirlo (y
            # puede servir menos de las que declara, igual que a nivel post).
            # Sin esto se sigue clickeando el botón del comentario SIGUIENTE
            # y se abren hilos ajenos al pedo.
            if ancla and not exp.get("pendientes") and not exp.get("clicks"):
                log.info(f"    el botón del hilo desapareció: Instagram sirvió "
                         f"{len(recibidas)} de {declaradas['n']} declaradas")
                break
            # El progreso son RESPUESTAS nuevas, nunca clicks: contar clicks
            # es lo que dejó el loop girando 2,5 h el 22/7.
            if nuevas:
                sin_novedad = 0
            else:
                sin_novedad += 1
                # Plan B si el ancla no sirve (cambió el href del permalink,
                # o el botón del hilo no está después del bloque enfocado):
                # se vuelve al barrido de todos los botones y se filtra por
                # padre, que es más caro pero no depende de la estructura.
                if ancla and not recibidas and (ajenas or not exp.get("ancla_ok")):
                    log.warning(f"    el ancla no dio con el botón del hilo "
                                f"(ancla_ok={exp.get('ancla_ok')}): se clickean todos")
                    ancla, limite, sin_novedad = None, 30, 0
                if not exp.get("clicks") and not exp.get("pendientes"):
                    break
                if sin_novedad >= max_sin_novedad:
                    break
                page.wait_for_timeout(min(1200 * sin_novedad, 6000))
            if pasadas >= MAX_PASADAS_POR_HILO:
                log.warning(f"    tope de {MAX_PASADAS_POR_HILO} pasadas en este hilo; "
                            f"se corta (el hilo puede quedar incompleto)")
                break
            s.delay(cfg["delay_entre_requests"])
    finally:
        page.close()

    if ajenas:
        log.info(f"    (se descartaron respuestas de {len(ajenas)} hilos ajenos "
                 f"que la página abrió por su cuenta)")
    if declaradas["n"] is not None and len(recibidas) < declaradas["n"]:
        log.warning(f"    hilo INCOMPLETO: {len(recibidas)} de {declaradas['n']} "
                    f"que declara Instagram")
    respuestas = [s.comentario_de_api(it, post, carga_id=carga_id, fase=3)
                  for it in recibidas.values()]
    return [c for c in respuestas if c]


# ---------------------------------------------------------------------------
# Estado / carga de raíces
# ---------------------------------------------------------------------------

def cargar_hechos() -> set:
    if hechos_file().exists():
        try:
            return set(json.loads(hechos_file().read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return set()


def guardar_hechos(hechos: set):
    hechos_file().write_text(json.dumps(sorted(hechos)), encoding="utf-8")


def cargar_raices_con_hilo(media_filtro: str | None) -> list[dict]:
    """Raíces (parent_pk None) con reply_count>0 y comentario_id real. Sin id
    real no se puede armar el permalink, así que esas se saltean (son de corridas
    viejas, previas a la captura del id)."""
    raices, sin_id = [], 0
    if not s.COMENTARIOS_FILE.exists():
        return raices
    with s.COMENTARIOS_FILE.open(encoding="utf-8") as f:
        for linea in f:
            try:
                c = json.loads(linea)
            except json.JSONDecodeError:
                continue
            if c.get("parent_pk") or not c.get("reply_count"):
                continue
            if media_filtro and str(c.get("media_id")) != str(media_filtro):
                continue
            if not c.get("comentario_id"):
                sin_id += 1
                continue
            raices.append(c)
    if sin_id:
        log.info(f"  ({sin_id} raíz con hilo pero sin comentario_id real; "
                 f"son de corridas viejas y se saltean)")
    return raices


def seleccionar_top_hilos(raices: list[dict], top: int) -> list[dict]:
    """Los `top` hilos con más respuestas declaradas de CADA post (top<=0 = todos).

    Sirve para muestrear posts grandes: bajar todos los hilos de un post con
    decenas de hilos y cientos de respuestas es más de una hora de clicks.

    El desempate es determinista (reply_count desc, posición en el orden servido
    asc, id) para que dos corridas sobre el mismo `.jsonl` elijan los mismos
    hilos: la muestra es reproducible.
    """
    if top <= 0:
        return raices
    por_post: dict = {}
    for r in raices:
        por_post.setdefault(str(r.get("media_id")), []).append(r)
    elegidos = []
    for media_id, grupo in por_post.items():
        grupo.sort(key=lambda r: (-int(r.get("reply_count") or 0),
                                  r.get("posicion_dom") if r.get("posicion_dom") is not None else 10**9,
                                  str(r.get("comentario_id"))))
        elegidos.extend(grupo[:top])
        desc = ", ".join(str(r.get("reply_count")) for r in grupo[:top])
        log.info(f"  post {media_id}: {len(grupo)} hilos con respuestas → "
                 f"se toman {min(top, len(grupo))} (declaran: {desc})")
    return elegidos


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Pasada 2: respuestas de los hilos por permalink")
    ap.add_argument("--media", help="procesar solo este media_id")
    ap.add_argument("--reset", action="store_true",
                    help="reprocesa hilos ya marcados como hechos")
    ap.add_argument("--limite", type=int, help="tope de hilos a procesar (para probar)")
    ap.add_argument("--top-hilos", type=int, default=TOP_HILOS_POR_POST,
                    help="hilos por post, los de más respuestas (default 0 = todos). "
                         "Es criterio de muestreo: cambiarlo cambia el corpus")
    ap.add_argument("--config", default=str(s.CONFIG_FILE),
                    help="Archivo de configuración (default: config.json)")
    ap.add_argument("--salida", default=None,
                    help="Carpeta de salida de la pasada 1 (default: data/)")
    ap.add_argument("--headful", action="store_true",
                    help="Muestra la ventana del navegador (para debuggear)")
    args = ap.parse_args()

    s.configurar_rutas(args.salida)
    s.configurar_logging("respuestas")
    log.info("=" * 60)
    log.info("Pasada 2 — respuestas de hilos (por permalink)")
    log.info("=" * 60)

    todas = cargar_raices_con_hilo(args.media)
    if not todas:
        log.error("No hay raíces con hilo y comentario_id real. ¿Corriste la pasada 1 nueva?")
        return

    # Muestreo de hilos ANTES del filtro por hechos: la selección tiene que
    # depender solo del corpus, no de lo que ya se haya bajado.
    if args.top_hilos > 0:
        log.info(f"Muestreo de hilos: los {args.top_hilos} de más respuestas por post")
    else:
        log.info("Muestreo de hilos: TODOS (--top-hilos 0)")
    raices = seleccionar_top_hilos(todas, args.top_hilos)

    hechos = set() if args.reset else cargar_hechos()
    pendientes = [r for r in raices if r["comentario_id"] not in hechos]
    if args.limite:
        pendientes = pendientes[: args.limite]

    total_resp = sum(r["reply_count"] for r in pendientes)
    log.info(f"Hilos en el corpus: {len(todas)} | seleccionados: {len(raices)} "
             f"| ya hechos: {len(raices) - len(pendientes)} "
             f"| a procesar: {len(pendientes)} (~{total_resp} respuestas declaradas)")
    if not pendientes:
        log.info("Nada pendiente. (Usá --reset para reprocesar.)")
        return

    cookies = s.cargar_sesion()
    cfg = s.cargar_config(args.config)
    vistos = s.cargar_pks_vistos()          # dedup global contra lo ya guardado
    log.info(f"Comentarios ya guardados (dedup): {len(vistos)}")

    n_hilos, n_resp, n_error = 0, 0, 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        context = s.crear_contexto_navegador(browser, cookies)
        try:
            for i, raiz in enumerate(pendientes, 1):
                post = {"media_id": raiz["media_id"], "shortcode": raiz["shortcode"],
                        "cuenta": raiz.get("cuenta_post")}
                etiqueta = f"{raiz['comentario_id']} (@{raiz.get('autor')}, "\
                           f"declara {raiz['reply_count']} resp)"
                log.info(f"\n[{i}/{len(pendientes)}] hilo {etiqueta}")
                try:
                    respuestas = respuestas_por_permalink(context, post, raiz, cfg)
                except s.BloqueoInstagram as e:
                    log.error(f"⚠ BLOQUEO: {e}")
                    log.error("Cortando. Los hilos ya hechos quedaron guardados; retomá más tarde.")
                    break
                except Exception as e:
                    log.error(f"  Error en el hilo {raiz['comentario_id']}: {e}")
                    n_error += 1
                    continue

                # Dedup global: no re-escribir respuestas ya guardadas.
                nuevas = []
                for c in respuestas:
                    if c["comentario_pk"] in vistos:
                        continue
                    vistos.add(c["comentario_pk"])
                    nuevas.append(c)

                s.append_jsonl(s.COMENTARIOS_FILE, nuevas)
                hechos.add(raiz["comentario_id"])
                guardar_hechos(hechos)          # persistencia por hilo: reintentable
                n_hilos += 1
                n_resp += len(nuevas)
                cob = len(respuestas) / raiz["reply_count"] if raiz["reply_count"] else 0
                log.info(f"  → {len(nuevas)} nuevas ({len(respuestas)} en el hilo, "
                         f"cobertura {cob:.0%} de las declaradas)")

                if i < len(pendientes):
                    s.delay(cfg["delay_entre_posts"])
        finally:
            browser.close()

    log.info("\n--- RESUMEN pasada 2 ---")
    log.info(f"Hilos procesados: {n_hilos}")
    log.info(f"Hilos con error:  {n_error}")
    log.info(f"Respuestas nuevas: {n_resp}")
    log.info(f"Output: {s.COMENTARIOS_FILE}")


if __name__ == "__main__":
    main()
