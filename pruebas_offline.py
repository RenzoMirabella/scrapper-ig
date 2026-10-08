#!/usr/bin/env python3
"""
Verificaciones OFFLINE del scraper. No tocan Instagram: arman un DOM sintético
que imita el panel de comentarios y corren contra él el JS y el parser reales.

    python pruebas_offline.py

Sirven para probar cambios sin gastar cargas de la cuenta. Lo que NO pueden
probar es que Instagram siga sirviendo el DOM como acá se lo imita: eso solo lo
dice una corrida real. Si Instagram cambia su UI, lo primero es volcar el HTML
real de un post y actualizar estas muestras.

Tarda ~1 minuto: la prueba de punta a punta paga el backoff real de la fase 1
(el scraper espera con paciencia creciente antes de dar un post por terminado,
y esa espera es justamente parte de lo que se quiere ejercitar).

Las etiquetas A1..A7 de los mensajes: A1 header de CSV, A2 resume,
A3 posicion_dom / carga_id / fase / origen, A4 evidencia de indentación,
A5 tiempos, A6 caption / taken_at / edad_post_horas, A7 sticker y verificado.

Todos los usuarios, textos e IDs de acá son inventados.
"""
import sys
import csv
import json
import tempfile
from pathlib import Path

import scraper_comentarios_ig as s
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

# El avatar del autor está en TODOS los comentarios: si contara como imagen, la
# señal de sticker no distinguiría nada.
AVATAR = '<img alt="Foto del perfil de {a}" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">'
PIXEL = 'data:image/gif;base64,R0lGODlhAQABAAAAACw='
BADGE = '<svg aria-label="Verificado"></svg>'


def _bloque(autor, texto, likes, cid, indent=0, respuestas=0, verif=False, sticker=False):
    """Un bloque de comentario con la forma que tiene en el DOM de Instagram."""
    extra = BADGE if verif else ""
    img = f'<img alt="Sticker de gato" src="{PIXEL}">' if sticker else ""
    resp = f"<div><span>Ver las {respuestas} respuestas</span></div>" if respuestas else ""
    cuerpo = f"<div>{texto}</div>" if texto else ""
    return f"""
    <div style="margin-left:{indent}px">
      {AVATAR.format(a=autor)}
      <div>{autor}</div>{extra}
      {cuerpo}{img}
      <div><a href="/p/ABC123/c/{cid}/"><time datetime="2026-07-25T13:00:00.000Z">2 d</time></a></div>
      <div>{likes} Me gusta</div>
      <div>Responder</div>
      {resp}
    </div>"""


# ---------------------------------------------------------------------------
# A1 / A2 — persistencia y resume (sin navegador)
# ---------------------------------------------------------------------------

def prueba_csv_y_resume(tmp: Path) -> list[str]:
    fallos = []

    # A1: agregar una columna tiene que CORTAR, no escribir la fila torcida.
    p = tmp / "t.csv"
    s.append_csv(p, [{"a": 1, "b": 2}])
    try:
        s.append_csv(p, [{"a": 1, "b": 2, "c": 3}])
        fallos.append("A1: append_csv escribió una fila con columnas de más")
    except RuntimeError:
        pass
    s.append_csv(p, [{"a": 9, "b": 8}])          # el header que sí coincide, sigue
    if len(p.read_text(encoding="utf-8-sig").strip().splitlines()) != 3:
        fallos.append("A1: append_csv rompió el caso normal")

    # A2: cuentan como resueltos ok_completo y ok_tope (corte pedido por
    # config, no truncación de IG). Los "ok_*" que son parciales de verdad
    # tienen que volver a la cola.
    est = tmp / "estado_resume.csv"
    with est.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["media_id", "estado"])
        w.writeheader()
        for mid, e in [("1", "ok_completo"), ("2", "ok_truncado_por_ig"),
                       ("3", "ok_corte_tiempo"), ("4", "ok_tope"),
                       ("5", "ok_corte_seguridad"), ("6", "error_inesperado")]:
            w.writerow({"media_id": mid, "estado": e})
    original, s.ESTADO_FILE = s.ESTADO_FILE, est
    resueltos = s.cargar_media_ids_resueltos()
    s.ESTADO_FILE = original
    if resueltos != {"1", "4"}:
        fallos.append(f"A2: resueltos esperados {{'1','4'}}, dio {resueltos}")

    # A6: la fórmula de la edad, con un "ahora" fijo.
    ahora = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    if s._edad_post_horas("2026-07-25T12:00:00.000Z", ahora) != 48.0:
        fallos.append("A6: edad_post_horas mal calculada para 48 h exactas")
    if s._edad_post_horas(None, ahora) is not None:
        fallos.append("A6: edad_post_horas no tolera taken_at ausente")
    if s._edad_post_horas("no es fecha", ahora) is not None:
        fallos.append("A6: edad_post_horas no tolera una fecha inválida")
    return fallos


# ---------------------------------------------------------------------------
# La cola acumula: config.json + pendientes, y el "listo" sale del .jsonl
# ---------------------------------------------------------------------------

def prueba_cola_acumulada(tmp: Path) -> list[str]:
    """
    Regresión: con cola pendiente, construir_cola devolvía la cola y NO leía
    config.json, así que una URL nueva no entraba nunca y no había ningún error.
    Pasaba justo después de un bloqueo, que es cuando la cola queda con
    pendientes.

    Cubre además el criterio de "resuelto" contando raíces del .jsonl, que es
    lo que permite dejar las URLs acumuladas en config.json para siempre.
    """
    fallos = []
    dir_t = tmp / "cola"
    dir_t.mkdir(exist_ok=True)

    PENDIENTE = "DAaaaaaaaaa"    # quedó a medias en la corrida anterior
    NUEVO     = "DBbbbbbbbbb"    # recién agregado a config
    HECHO     = "DXyzAbCdEfG"    # ya llegó al tope en corridas anteriores
    mid = lambda sc: str(s.shortcode_a_media_id(sc))

    (dir_t / "cola.json").write_text(json.dumps([{
        "cuenta": None, "shortcode": PENDIENTE, "media_id": mid(PENDIENTE),
        "caption": None, "comment_count": None, "taken_at": None,
    }]), encoding="utf-8")

    # El post HECHO se bajó en DOS corridas de 3 raíces: ninguna fila de
    # estado.csv llega al tope de 5, solo la suma. Si "resuelto" se leyera de
    # estado.csv, este post se reabriría todos los días para siempre.
    est = dir_t / "estado.csv"
    with est.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["media_id", "estado", "n_extraidos"])
        w.writeheader()
        w.writerow({"media_id": mid(HECHO), "estado": "ok_corte_tiempo", "n_extraidos": 3})
        w.writerow({"media_id": mid(HECHO), "estado": "ok_corte_tiempo", "n_extraidos": 3})

    jsonl = dir_t / "comentarios.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for i in range(6):
            f.write(json.dumps({"comentario_pk": f"h{i}", "media_id": mid(HECHO),
                                "parent_pk": None}) + "\n")
        # Y el pendiente tiene 2 raíces + 9 respuestas: las respuestas NO
        # cuentan para el tope (las baja la pasada 2 al mismo archivo).
        for i in range(2):
            f.write(json.dumps({"comentario_pk": f"p{i}", "media_id": mid(PENDIENTE),
                                "parent_pk": None}) + "\n")
        for i in range(9):
            f.write(json.dumps({"comentario_pk": f"r{i}", "media_id": mid(PENDIENTE),
                                "parent_pk": "p0"}) + "\n")

    cfg = dict(s.DEFAULTS)
    cfg.update({
        "cuentas": ["cuenta_ejemplo"],       # con sesion=None: si intenta descubrir, rompe
        "post_urls": [f"https://www.instagram.com/p/{sc}/?hl=es-la"
                      for sc in (PENDIENTE, NUEVO, HECHO)],
        "max_comentarios_por_post": 5,
        "delay_entre_posts": [0, 0],
    })

    orig = (s.COLA_FILE, s.ESTADO_FILE, s.COMENTARIOS_FILE, s.POSTS_FILE)
    s.COLA_FILE, s.ESTADO_FILE = dir_t / "cola.json", est
    s.COMENTARIOS_FILE, s.POSTS_FILE = jsonl, dir_t / "posts.csv"
    try:
        cola = s.construir_cola(None, cfg)
    except Exception as e:
        s.COLA_FILE, s.ESTADO_FILE, s.COMENTARIOS_FILE, s.POSTS_FILE = orig
        return [f"cola: construir_cola explotó ({e!r}); "
                f"¿intentó descubrir por cuenta habiendo cola pendiente?"]
    persistida = json.loads((dir_t / "cola.json").read_text(encoding="utf-8"))
    s.COLA_FILE, s.ESTADO_FILE, s.COMENTARIOS_FILE, s.POSTS_FILE = orig

    scs = [p["shortcode"] for p in cola]
    if NUEVO not in scs:
        fallos.append("cola: la URL nueva de config.json NO entró habiendo cola pendiente")
    if PENDIENTE not in scs:
        fallos.append("cola: se perdió el post que estaba pendiente")
    if HECHO in scs:
        fallos.append("cola: el post que ya tiene el tope de raíces en el .jsonl se reabrió")
    if len(scs) != len(set(scs)):
        fallos.append(f"cola: hay posts duplicados ({scs})")
    if [p["shortcode"] for p in persistida] != scs:
        fallos.append("cola: lo persistido no coincide con lo devuelto")

    # El pendiente tiene 11 comentarios pero solo 2 raíces: con tope 5 sigue
    # pendiente. Si las respuestas contaran, ya estaría "resuelto" en falso.
    s.COMENTARIOS_FILE, s.ESTADO_FILE = jsonl, est
    conteos = s.contar_raices_por_media()
    resueltos = s.cargar_media_ids_resueltos(5)
    s.COMENTARIOS_FILE, s.ESTADO_FILE = orig[2], orig[1]
    if conteos.get(mid(PENDIENTE)) != 2:
        fallos.append(f"cola: las respuestas cuentan como raíces "
                      f"({conteos.get(mid(PENDIENTE))} en vez de 2)")
    if mid(PENDIENTE) in resueltos:
        fallos.append("cola: un post con 2 raíces quedó resuelto contra un tope de 5")
    return fallos


# ---------------------------------------------------------------------------
# A3 / A4 / A7 — extracción de un panel sintético
# ---------------------------------------------------------------------------

def prueba_extraccion(browser) -> list[str]:
    html = f"""<body><div style="overflow-y:auto;height:200px"><div style="height:900px">
      {_bloque("ana", "hola qué tal", 5, 1, respuestas=2)}
      {_bloque("beto", "una respuesta", 1, 2, indent=40)}
      {_bloque("cata", "", 25, 3, sticker=True)}
      {_bloque("diego", "cuenta oficial", 99, 4, verif=True)}
    </div></div></body>"""

    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(html)
    page.evaluate(s._JS_INIT_CONTENEDOR)
    nuevos: list[dict] = []
    s._extraer_del_dom(page, {"media_id": "999", "shortcode": "ABC123", "cuenta": "test"},
                       set(), nuevos, {}, None, None,
                       carga_id="deadbeef", fase=1, origen="panel")
    page.close()

    fallos = []
    por_autor = {c["autor"]: c for c in nuevos}
    if len(nuevos) != 4:
        return [f"A3: se esperaban 4 comentarios, hubo {len(nuevos)}"]

    pos = [c["posicion_dom"] for c in nuevos]
    if pos != sorted(pos) or len(set(pos)) != len(pos):
        fallos.append(f"A3: posicion_dom no es estrictamente creciente: {pos}")
    if any(c["carga_id"] != "deadbeef" or c["fase"] != 1 or c["origen"] != "panel"
           for c in nuevos):
        fallos.append("A3: carga_id/fase/origen no se propagaron")
    if any(c["x_bloque"] is None or c["umbral_indentacion"] is None or c["min_x"] is None
           for c in nuevos):
        fallos.append("A4: falta la evidencia de indentación")
    if por_autor["beto"]["parent_pk"] is None:
        fallos.append("A4: el bloque indentado no quedó como respuesta")
    if por_autor["ana"]["parent_pk"] is not None:
        fallos.append("A4: un bloque no indentado quedó como respuesta")
    if por_autor["diego"]["autor_verificado"] is not True:
        fallos.append("A7: no se detectó el badge de verificado")
    if por_autor["ana"]["autor_verificado"] is not False:
        fallos.append("A7: falso positivo de verificado")
    if por_autor["cata"]["tiene_imagen"] is not True:
        fallos.append("A7: no se detectó el sticker")
    if por_autor["ana"]["tiene_imagen"] is not False:
        fallos.append("A7: el avatar del autor se contó como imagen")
    if por_autor["ana"]["reply_count"] != 2:
        fallos.append("reply_count mal parseado")
    if por_autor["ana"]["comentario_id"] != "1":
        fallos.append("comentario_id no salió del permalink")
    return fallos


# ---------------------------------------------------------------------------
# Umbral de indentación con el bloque del POST presente (página de permalink)
# ---------------------------------------------------------------------------

def prueba_umbral_con_bloque_post(browser) -> list[str]:
    """
    En la página de permalink el bloque del post tiene <time> pero no "Responder",
    y su x es MENOR que la de los comentarios. Si entra en el cálculo del mínimo,
    el umbral baja y las raíces se clasifican como respuesta: ninguna encuentra
    padre y la pasada 2 devuelve cero. Reproduce las x medidas el 2026-07-28
    (post 716 / raíz 732 / respuesta 786).
    """
    # El bloque del post: <time> sí, "Responder" no, y el margen más chico.
    post_html = """
    <div style="margin-left:0px">
      <div>cuenta_ejemplo</div>
      <div>https://example.com/un-link-del-post</div>
      <div><time datetime="2026-07-25T12:00:00.000Z">2 d</time></div>
    </div>"""
    cuerpo = (post_html
              + _bloque("raiz_uno", "comentario raiz uno", 10, 5001, indent=16)
              + _bloque("resp_a", "respuesta a", 1, 5002, indent=70)
              + _bloque("resp_b", "respuesta b", 2, 5003, indent=70)
              + _bloque("raiz_dos", "comentario raiz dos", 20, 5004, indent=16)
              + _bloque("resp_c", "respuesta c", 3, 5005, indent=70))
    html = f"""<html><head>
      <meta property="og:description" content='10 likes, 6 comments - cuenta_ejemplo el July 25, 2026'>
      <meta property="og:url" content="https://www.instagram.com/cuenta_ejemplo/p/ABC123/">
    </head><body><div style="overflow-y:auto;height:300px">{cuerpo}
      <div style="height:900px"></div></div></body></html>"""

    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(html)
    page.evaluate(s._JS_INIT_CONTENEDOR)
    post = {"media_id": "999", "shortcode": "ABC123", "cuenta": "cuenta_ejemplo"}
    vistos, extraidos, id_a_pk = set(), [], {}
    s._extraer_del_dom(page, post, vistos, extraidos, id_a_pk, None, None,
                       carga_id="test", fase=3, origen="permalink")
    page.close()

    fallos = []
    por_autor = {c["autor"]: c for c in extraidos}
    for autor in ("raiz_uno", "raiz_dos", "resp_a", "resp_b", "resp_c"):
        if autor not in por_autor:
            fallos.append(f"umbral: no se extrajo {autor}")
    if fallos:
        return fallos
    # el bloque del post no es un comentario: no debe aparecer
    if "cuenta_ejemplo" in por_autor:
        fallos.append("umbral: el bloque del post se extrajo como comentario")
    for autor in ("raiz_uno", "raiz_dos"):
        if por_autor[autor]["parent_pk"] is not None:
            fallos.append(f"umbral: {autor} es raíz pero quedó como respuesta "
                          f"(el bloque del post contaminó el mínimo)")
    for hijo, padre in (("resp_a", "raiz_uno"), ("resp_b", "raiz_uno"),
                        ("resp_c", "raiz_dos")):
        esperado = por_autor[padre]["comentario_pk"]
        if por_autor[hijo]["parent_pk"] != esperado:
            fallos.append(f"umbral: {hijo} deberia colgar de {padre}, "
                          f"quedó con parent_pk={por_autor[hijo]['parent_pk']}")
    return fallos


# ---------------------------------------------------------------------------
# A6 — el parser de caption, contra cadenas reales (sin navegador)
# ---------------------------------------------------------------------------

def prueba_caption() -> list[str]:
    """
    La primera cadena tiene la forma REAL del meta de Instagram (post público
    de @nasa). Termina en '". ' y ese punto es lo que rompía el parser. Las dos
    siguientes son posts sin caption: tienen que dar None sin inventar nada.
    """
    casos = [
        ('118K likes, 430 comments - nasa el July 21, 2026: '
         '"Two actively merging sub-clusters, both alike in mass". ',
         "Two actively merging sub-clusters, both alike in mass"),
        ('79K likes, 9,805 comments - cuenta_ejemplo el July 20, 2026', None),
        ('8,039 likes, 1,065 comments - cuenta_ejemplo el July 10, 2026', None),
        # el greedy tiene que anclar en la última comilla, no en la primera
        ('10 likes, 2 comments - x el July 1, 2026: "dijo "hola" y se fue". ',
         'dijo "hola" y se fue'),
        # un caption que termina en puntos no debe perderlos
        ('10 likes, 2 comments - x el July 1, 2026: "pero bueno...". ',
         "pero bueno..."),
        ('10 likes, 2 comments - x el July 1, 2026: "linea1\nlinea2". ',
         "linea1\nlinea2"),
        ('10 likes, 2 comments - x el July 1, 2026: "". ', None),
        (None, None),
        ("", None),
    ]
    fallos = []
    for desc, esperado in casos:
        obtenido = s._caption_de_meta(desc)
        if obtenido != esperado:
            fallos.append(f"A6 caption: {desc!r:60} -> {obtenido!r}, "
                          f"se esperaba {esperado!r}")
    return fallos


# ---------------------------------------------------------------------------
# El paginador de respuestas: "Ver respuestas (N)" descuenta y NO desaparece
# ---------------------------------------------------------------------------

def prueba_paginador_respuestas(browser) -> list[str]:
    """
    Regresión de los DOS bugs opuestos del expansor de hilos:

    a) marcar el botón por NODO (`data-scrp-exp="1"`) hacía que "Ver respuestas
       (N)" —que es el paginador y sigue ahí, descontando— se clickeara una sola
       vez: 3 respuestas por hilo y nunca más (medido 2026-07-28).
    b) NO marcar nada hacía que un botón que Instagram ya no responde se
       re-clickeara para siempre (2,5 h el 22/7).

    El DOM sintético imita el comportamiento medido en vivo: cada click entrega
    3 respuestas y el contador baja ("(9)" → "(6)" → ...). Al agotarse, el botón
    QUEDA en el DOM diciendo "(0)" — que es el caso que tiene que cortar.
    """
    html = """<body>
      <div id="hilo"><div><span id="pag">Ver respuestas (9)</span></div></div>
      <script>
        window.__restantes = 9;
        document.getElementById('pag').addEventListener('click', () => {
          const n = Math.min(3, window.__restantes);
          window.__restantes -= n;
          for (let i = 0; i < n; i++) {
            const d = document.createElement('div');
            d.className = 'resp';
            document.getElementById('hilo').appendChild(d);
          }
          document.getElementById('pag').textContent =
            'Ver respuestas (' + window.__restantes + ')';
        });
      </script>
    </body>"""

    fallos = []
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(html)
    clicks_totales, pasadas_hasta_agotar = 0, None
    for i in range(15):
        exp = page.evaluate(s._JS_EXPANDIR_RESPUESTAS, {"limite": 30, "modo": "documento"})
        clicks_totales += exp["clicks"]
        if exp["clicks"] == 0 and pasadas_hasta_agotar is None:
            pasadas_hasta_agotar = i + 1
            if not exp["agotados"]:
                fallos.append("paginador: dejó de clickear sin marcar el botón como agotado")
            if exp["pendientes"]:
                fallos.append("paginador: el botón agotado sigue contando como pendiente")
    bajadas = page.evaluate("() => document.querySelectorAll('.resp').length")
    page.close()

    if bajadas != 9:
        fallos.append(f"paginador: se bajaron {bajadas} respuestas de 9 "
                      f"(el botón se clickeó {clicks_totales} veces)")
    if pasadas_hasta_agotar is None:
        fallos.append("paginador: nunca cortó — el botón se re-clickea para siempre")
    elif pasadas_hasta_agotar > 10:
        fallos.append(f"paginador: tardó {pasadas_hasta_agotar} pasadas en darse por agotado")

    # El paginador SIN contador ("Mostrar más respuestas", panel del post) no se
    # puede evaluar por su texto: tiene que seguir clickeable en cada pasada. Lo
    # que corta ahí es el llamador, contando comentarios nuevos.
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content('<body><div><span>Mostrar más respuestas</span></div></body>')
    clicks = [page.evaluate(s._JS_EXPANDIR_RESPUESTAS, {"limite": 30, "modo": "documento"})["clicks"] for _ in range(5)]
    page.close()
    if clicks != [1, 1, 1, 1, 1]:
        fallos.append(f"paginador sin contador: se dejó de clickear ({clicks}); "
                      f"en el panel del post eso trunca los hilos")
    return fallos


# ---------------------------------------------------------------------------
# El hilo del permalink cae FUERA del panel scrolleable
# ---------------------------------------------------------------------------

def prueba_raiz_documento_en_permalink(browser) -> list[str]:
    """
    Tercer motivo del "0 respuestas" de la pasada 2: en la página de permalink,
    `panelComentarios()` elige el contenedor scrolleable de *otros* comentarios
    del post, y el hilo enfocado queda AFUERA. Medido en vivo 2026-07-28: 16
    `<time>` en el panel elegido contra 50 en el documento, y la extracción
    devolvía solo raíces por más que el hilo estuviera abierto.

    El DOM sintético reproduce esa forma: el hilo (raíz + respuestas + el botón
    de paginar) fuera del panel, y el panel scrolleable con otros comentarios.
    Con `origen="permalink"` la extracción lee el documento entero y las ve; el
    test comprueba también que leyendo el panel NO las ve — si no, no probaría
    nada.
    """
    post_html = """
    <div style="margin-left:0px">
      <div>cuenta_ejemplo</div>
      <div><time datetime="2026-07-25T12:00:00.000Z">2 d</time></div>
    </div>"""
    hilo = (post_html
            + _bloque("raiz_hilo", "el comentario enfocado", 40, 6001, indent=16)
            + _bloque("resp_uno", "primera respuesta", 2, 6002, indent=70)
            + _bloque("resp_dos", "segunda respuesta", 3, 6003, indent=70)
            + '<div><span>Ver respuestas (2)</span></div>')
    otros = "".join(_bloque(f"otro{i}", f"comentario ajeno {i}", i, 7000 + i, indent=16)
                    for i in range(3))
    html = f"""<html><head>
      <meta property="og:url" content="https://www.instagram.com/cuenta_ejemplo/p/ABC123/">
    </head><body>
      <div id="hilo">{hilo}</div>
      <div style="overflow-y:auto;height:200px">{otros}<div style="height:900px"></div></div>
    </body></html>"""

    post = {"media_id": "999", "shortcode": "ABC123", "cuenta": "cuenta_ejemplo"}
    fallos = []
    extraido_por_origen = {}
    for origen in ("permalink", "panel"):
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(html)
        page.evaluate(s._JS_INIT_CONTENEDOR)
        nuevos: list[dict] = []
        s._extraer_del_dom(page, post, set(), nuevos, {}, None, None,
                           carga_id="test", fase=3, origen=origen)
        modo = "documento" if origen == "permalink" else "panel"
        clicks = page.evaluate(s._JS_EXPANDIR_RESPUESTAS,
                               {"limite": 30, "modo": modo})["clicks"]
        page.close()
        extraido_por_origen[origen] = ({c["autor"]: c for c in nuevos}, clicks)

    por_autor, clicks = extraido_por_origen["permalink"]
    for autor in ("raiz_hilo", "resp_uno", "resp_dos", "otro0", "otro2"):
        if autor not in por_autor:
            fallos.append(f"permalink: no se extrajo {autor} (¿se leyó solo el panel?)")
    if not fallos:
        esperado = por_autor["raiz_hilo"]["comentario_pk"]
        for hijo in ("resp_uno", "resp_dos"):
            if por_autor[hijo]["parent_pk"] != esperado:
                fallos.append(f"permalink: {hijo} no quedó colgando del raíz del hilo "
                              f"(parent_pk={por_autor[hijo]['parent_pk']})")
        if por_autor["otro0"]["parent_pk"] is not None:
            fallos.append("permalink: un comentario ajeno quedó como respuesta del hilo")
    if clicks != 1:
        fallos.append(f"permalink: el paginador fuera del panel no se clickeó ({clicks})")

    # Control: leyendo solo el panel, el hilo es invisible. Si esto encontrara
    # las respuestas, la prueba de arriba pasaría igual con el bug puesto.
    por_autor_panel, clicks_panel = extraido_por_origen["panel"]
    if "resp_uno" in por_autor_panel or clicks_panel:
        fallos.append("la prueba no discrimina: el panel scrolleable también ve el hilo")
    return fallos


# ---------------------------------------------------------------------------
# El botón del hilo se elige por ancla, no por ser el primero de la página
# ---------------------------------------------------------------------------

def prueba_ancla_del_hilo(browser) -> list[str]:
    """
    La página de permalink trae el comentario enfocado Y los otros comentarios
    del post, cada uno con su "Ver respuestas (N)". Clickear el que toca no es
    cosmético: cada click de más pide a Instagram un hilo que no vamos a
    guardar, y cuando el hilo enfocado se agota su botón desaparece — sin ancla,
    el "primer botón" pasa a ser el del comentario siguiente y la pasada sigue
    abriendo hilos ajenos (medido 2026-07-28: 4 pasadas de más y un hilo ajeno
    tocado).
    """
    html = """<body>
      <div><a href="/p/ABC123/c/111/?hl=es-la"><time datetime="2026-07-25T13:00:00.000Z">2 d</time></a></div>
      <div><span id="mio">Ver respuestas (5)</span></div>
      <div><a href="/p/ABC123/c/999/?hl=es-la"><time datetime="2026-07-25T13:05:00.000Z">2 d</time></a></div>
      <div><span id="ajeno">Ver respuestas (7)</span></div>
      <script>
        window.__clickeados = [];
        for (const id of ['mio', 'ajeno']) {
          document.getElementById(id).addEventListener('click', () => window.__clickeados.push(id));
        }
      </script>
    </body>"""
    fallos = []
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(html)

    exp = page.evaluate(s._JS_EXPANDIR_RESPUESTAS,
                        {"limite": 30, "modo": "documento", "ancla": "111"})
    clickeados = page.evaluate("() => window.__clickeados")
    if clickeados != ["mio"]:
        fallos.append(f"ancla: se clickeó {clickeados}, se esperaba solo el del hilo")
    if exp.get("pendientes") != 1:
        fallos.append(f"ancla: pendientes={exp.get('pendientes')}, con ancla debería ser 1")

    # El hilo se agota y su botón desaparece: no hay que caer en el del vecino.
    page.evaluate("() => { document.getElementById('mio').remove(); window.__clickeados = []; }")
    exp2 = page.evaluate(s._JS_EXPANDIR_RESPUESTAS,
                         {"limite": 30, "modo": "documento", "ancla": "111"})
    if page.evaluate("() => window.__clickeados"):
        fallos.append("ancla: sin el botón del hilo se clickeó el del comentario siguiente")
    if exp2.get("clicks") or exp2.get("pendientes"):
        fallos.append(f"ancla: hilo agotado pero devuelve clicks={exp2.get('clicks')} "
                      f"pendientes={exp2.get('pendientes')} (el llamador no corta)")

    # Ancla que no existe: se avisa, no se clickea a ciegas.
    exp3 = page.evaluate(s._JS_EXPANDIR_RESPUESTAS,
                         {"limite": 30, "modo": "documento", "ancla": "nohay"})
    if exp3.get("ancla_ok") is not False or exp3.get("clicks"):
        fallos.append("ancla: con un ancla inexistente no se avisa (ancla_ok) o se clickea igual")
    page.close()
    return fallos


# ---------------------------------------------------------------------------
# Mapeo del JSON de respuestas al esquema del .jsonl
# ---------------------------------------------------------------------------

def prueba_comentario_de_api() -> list[str]:
    """
    Item con la forma REAL de /api/v1/media/<media>/comments/<raiz>/child_comments/
    (mismos campos y tipos que devuelve Instagram; valores inventados). La
    forma se copia de una respuesta real y no se escribe de memoria: una muestra
    inventada a mano es lo que hizo que el test del caption diera verde mientras
    el parser fallaba contra todo post real.
    """
    item = {
        "pk": "18000000000000001",
        "parent_comment_id": "18000000000000000",
        "media_id": "3900000000000000000",
        "child_comment_index": 0,
        "comment_like_count": 17,
        "created_at": 1783695035,
        "created_at_utc": 1783695035,
        "text": "@otro_usuario no estoy de acuerdo",
        "type": 2,
        "user_id": 1000000001,
        "user": {"pk": "1000000001", "username": "usuario_de_prueba",
                 "is_verified": False, "full_name": "Usuario De Prueba",
                 "profile_pic_url": "https://…"},
    }
    post = {"media_id": "3900000000000000000", "shortcode": "DBbbbbbbbbb",
            "cuenta": "cuenta_ejemplo"}
    c = s.comentario_de_api(item, post, carga_id="abc123", fase=3)
    fallos = []
    esperado = {
        "comentario_pk": "18000000000000001",
        "comentario_id": "18000000000000001",
        "parent_pk": "18000000000000000",     # dato, no inferencia por indentación
        "autor": "usuario_de_prueba",
        "autor_pk": "1000000001",             # el ID numérico que el DOM no da
        "autor_verificado": False,
        "like_count": 17,
        "posicion_dom": 0,                    # child_comment_index = orden servido
        "created_at": "2026-07-10T14:50:35.000Z",
        "origen": "api_child_comments",
        "fase": 3,
        "shortcode": "DBbbbbbbbbb",
    }
    for k, v in esperado.items():
        if c.get(k) != v:
            fallos.append(f"api: {k} = {c.get(k)!r}, se esperaba {v!r}")
    # Mismo esquema que el camino por DOM: si divergen, el .jsonl deja de ser
    # una sola tabla.
    del_dom = s._parsear_bloque_comentario(
        ["autor", "texto", "2 d5 Me gustaResponder"], "2026-07-10T14:50:35.000Z", post,
        href="/p/DBbbbbbbbbb/c/123/", carga_id="abc123", fase=3, origen="permalink")
    if set(c) != set(del_dom):
        fallos.append(f"api: el esquema no coincide con el del DOM: "
                      f"sobran {set(c) - set(del_dom)}, faltan {set(del_dom) - set(c)}")
    # La señal de imagen no existe en el JSON: tiene que ser None (desconocido),
    # no False (que afirmaría que no hay imagen).
    if c["tiene_imagen"] is not None:
        fallos.append("api: tiene_imagen debería quedar en None, no en False")
    if s.comentario_de_api({"text": "sin pk"}, post) is not None:
        fallos.append("api: un item sin pk debería descartarse")
    return fallos


# ---------------------------------------------------------------------------
# Muestreo de hilos — los N de más respuestas por post
# ---------------------------------------------------------------------------

def prueba_top_hilos() -> list[str]:
    """El muestreo de hilos tiene que ser por post y reproducible."""
    import pasada_respuestas as pr

    def raiz(mid, cid, n, pos):
        return {"media_id": mid, "comentario_id": cid, "reply_count": n,
                "posicion_dom": pos}

    raices = [raiz("A", "a1", 5, 0), raiz("A", "a2", 40, 1), raiz("A", "a3", 12, 2),
              raiz("B", "b1", 7, 0), raiz("B", "b2", 7, 1),
              raiz("C", "c1", 3, 0)]
    fallos = []
    top2 = pr.seleccionar_top_hilos(list(raices), 2)
    ids = [r["comentario_id"] for r in top2]
    if sorted(ids) != ["a2", "a3", "b1", "b2", "c1"]:
        fallos.append(f"top-hilos: selección inesperada: {ids}")
    # Empate resuelto por posición servida: b1 antes que b2, siempre igual.
    if [r["comentario_id"] for r in pr.seleccionar_top_hilos(list(reversed(raices)), 2)
        if r["media_id"] == "B"] != ["b1", "b2"]:
        fallos.append("top-hilos: el desempate depende del orden de entrada")
    if len(pr.seleccionar_top_hilos(list(raices), 0)) != len(raices):
        fallos.append("top-hilos: --top-hilos 0 tiene que devolver todos")
    if [r["comentario_id"] for r in pr.seleccionar_top_hilos(list(raices), 1)
        if r["media_id"] == "A"] != ["a2"]:
        fallos.append("top-hilos: no eligió el hilo más grande del post")
    return fallos


# ---------------------------------------------------------------------------
# A6 — meta del post, con y sin panel scrolleable
# ---------------------------------------------------------------------------

def prueba_meta(browser) -> list[str]:
    # El '. ' del final NO es decorativo: es como Instagram cierra el string de
    # verdad. Esta muestra terminaba en '"' y por eso el test daba verde
    # mientras el parser devolvía None contra los posts reales (2026-07-28).
    desc = ('74K likes, 8,679 comments - cuenta_ejemplo el July 25, 2026: '
            '"Un caption de ejemplo. Con punto final!". ')
    # El post se publicó a las 12:00Z; los comentarios son posteriores. El
    # criterio "el <time> más viejo" es lo que tiene que salvar el caso sin
    # panel, donde no se puede filtrar por contenedor.
    html = f"""<head>
      <meta property="og:description" content='{desc}'>
      <meta property="og:url" content="https://www.instagram.com/cuenta_ejemplo/p/ABC123/">
    </head><body>
      <header><time datetime="2026-07-25T12:00:00.000Z">2 d</time></header>
      <div id="panel" style="overflow-y:auto;height:100px"><div style="height:900px">
        <div><time datetime="2026-07-25T13:30:00.000Z">1 d</time></div>
        <div><time datetime="2026-07-26T09:00:00.000Z">1 d</time></div>
      </div></div>
    </body>"""

    fallos = []
    for etiqueta, doc in [("con panel", html),
                          ("sin panel", html.replace('style="overflow-y:auto;height:100px"', ""))]:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(doc)
        page.evaluate(s._JS_INIT_CONTENEDOR)
        meta = s.extraer_meta_post(page, {"media_id": "999", "shortcode": "ABC123"})
        page.close()
        if meta["taken_at"] != "2026-07-25T12:00:00.000Z":
            fallos.append(f"A6 ({etiqueta}): taken_at mal: {meta['taken_at']}")
        if meta["caption"] != "Un caption de ejemplo. Con punto final!":
            fallos.append(f"A6 ({etiqueta}): caption mal: {meta['caption']!r}")
        if meta["post_comment_count"] != 8679:
            fallos.append(f"A6 ({etiqueta}): post_comment_count mal")
        if meta["cuenta"] != "cuenta_ejemplo":
            fallos.append(f"A6 ({etiqueta}): la cuenta no salió de og:url")
    return fallos


# ---------------------------------------------------------------------------
# Punta a punta — fase 1 completa, estado.csv y comentarios.jsonl
# ---------------------------------------------------------------------------

def prueba_punta_a_punta(browser, tmp: Path) -> list[str]:
    cuerpo = "".join(
        _bloque(f"user{i:03d}", f"comentario numero {i}", i, 9000 + i,
                indent=40 if i and i % 7 == 0 else 0)
        for i in range(30)
    )
    html = f"""<html><head>
      <meta property="og:description" content='1,234 likes, 30 comments - cuenta_ejemplo el July 25, 2026: "Caption de prueba"'>
      <meta property="og:url" content="https://www.instagram.com/cuenta_ejemplo/p/ABC123/">
    </head><body>
      <header><time datetime="2026-07-25T12:00:00.000Z">2 d</time></header>
      <div style="overflow-y:auto;height:300px">{cuerpo}<div style="height:1200px"></div></div>
    </body></html>"""
    (tmp / "post.html").write_text(html, encoding="utf-8")

    # Redirigir las salidas al temp y neutralizar los delays entre requests.
    guardados = (s.COMENTARIOS_FILE, s.ESTADO_FILE, s.construir_url_post, s.delay,
                 s.CAPTURAS_DIR)
    s.COMENTARIOS_FILE = tmp / "comentarios.jsonl"
    s.ESTADO_FILE = tmp / "estado.csv"
    # Sin esto la captura del post cae en el data/ REAL: este test corre un
    # navegador de verdad y guardar_captura_post() escribe igual que en producción.
    s.CAPTURAS_DIR = tmp / "capturas"
    s.CAPTURAS_DIR.mkdir(parents=True, exist_ok=True)
    s.construir_url_post = lambda post: (tmp / "post.html").as_uri()
    s.delay = lambda rango: None
    try:
        cfg = dict(s.DEFAULTS)
        cfg["max_minutos_por_post"] = 2
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        post = {"media_id": "111", "shortcode": "ABC123", "cuenta": None,
                "caption": None, "comment_count": None, "taken_at": None}
        comentarios, estado = s.scrape_comentarios_post_navegador(
            ctx, post, cfg, set(),
            persistir=lambda lote: s.append_jsonl(s.COMENTARIOS_FILE, lote))
        ctx.close()
        s.append_csv(s.ESTADO_FILE, [estado])

        fallos = []
        if len(comentarios) != 30:
            fallos.append(f"e2e: se esperaban 30 comentarios, hubo {len(comentarios)}")
        if estado["estado"] != "ok_completo":
            fallos.append(f"e2e: estado inesperado: {estado['estado']}")
        if estado["taken_at"] != "2026-07-25T12:00:00.000Z":
            fallos.append(f"e2e: taken_at mal: {estado['taken_at']}")
        if estado["caption"] != "Caption de prueba":
            fallos.append(f"e2e: caption mal: {estado['caption']!r}")
        if estado["edad_post_horas"] is None:
            fallos.append("e2e: falta edad_post_horas")
        if None in (estado["t_fin"], estado["duracion_seg"], estado["iteraciones"],
                    estado["pasadas_fase2"], estado["t_inicio"]):
            fallos.append("A5: faltan tiempos o esfuerzo en estado.csv")
        if estado["n_respuestas"] != 4:
            fallos.append(f"e2e: n_respuestas esperado 4, dio {estado['n_respuestas']}")
        if estado["cobertura"] != 1.0:
            fallos.append(f"e2e: cobertura esperada 1.0, dio {estado['cobertura']}")

        pos = [c["posicion_dom"] for c in comentarios]
        if pos != sorted(pos) or len(set(pos)) != len(pos):
            fallos.append("A3: posicion_dom no es estrictamente creciente en la corrida")
        if len({c["carga_id"] for c in comentarios}) != 1:
            fallos.append("A3: más de un carga_id en una sola carga de página")
        if estado["carga_id"] != comentarios[0]["carga_id"]:
            fallos.append("A3: el carga_id de estado.csv no coincide con el de los comentarios")

        with s.ESTADO_FILE.open(encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f))
        if header != list(estado.keys()):
            fallos.append("A1: el header de estado.csv no coincide con las claves")
        if s.cargar_media_ids_resueltos() != {"111"}:
            fallos.append("A2: el resume no reconoció el post completo")

        n_lineas = sum(1 for _ in s.COMENTARIOS_FILE.open(encoding="utf-8"))
        if n_lineas != 30:
            fallos.append(f"e2e: el jsonl tiene {n_lineas} líneas, se esperaban 30")
        return fallos
    finally:
        (s.COMENTARIOS_FILE, s.ESTADO_FILE, s.construir_url_post, s.delay,
         s.CAPTURAS_DIR) = guardados


def main():
    tmp = Path(tempfile.mkdtemp(prefix="ig_pruebas_"))
    print(f"temp: {tmp}\n")
    fallos = []
    fallos += prueba_csv_y_resume(tmp)
    print("  [1/12] persistencia y resume")
    fallos += prueba_caption()
    print("  [2/12] parser de caption")
    fallos += prueba_top_hilos()
    print("  [3/12] muestreo de hilos (top N por post)")
    fallos += prueba_comentario_de_api()
    print("  [4/12] mapeo del JSON de respuestas")
    fallos += prueba_cola_acumulada(tmp)
    print("  [5/12] la cola acumula config + pendientes")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            fallos += prueba_extraccion(browser)
            print("  [6/12] extracción del panel")
            fallos += prueba_umbral_con_bloque_post(browser)
            print("  [7/12] umbral con el bloque del post presente")
            fallos += prueba_paginador_respuestas(browser)
            print("  [8/12] paginador de respuestas")
            fallos += prueba_ancla_del_hilo(browser)
            print("  [9/12] el botón del hilo se elige por ancla")
            fallos += prueba_raiz_documento_en_permalink(browser)
            print("  [10/12] el hilo del permalink, fuera del panel")
            fallos += prueba_meta(browser)
            print("  [11/12] meta del post")
            print("  [12/12] punta a punta (~1 min, paga el backoff real)...")
            fallos += prueba_punta_a_punta(browser, tmp)
        finally:
            browser.close()

    if fallos:
        print("\nFALLOS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("\nTodo OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
