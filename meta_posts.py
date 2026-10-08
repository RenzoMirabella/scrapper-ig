#!/usr/bin/env python3
"""Metadatos de uno o varios posts, sin bajar comentarios.

Abre cada post y lee lo que Instagram expone en la página: hora de publicación
(`taken_at`), likes y comentarios declarados, caption y cuenta. Cuesta **una
navegación por post y ningún scroll**, así que sirve para decidir qué posts
scrapear (o en qué orden) antes de gastar una corrida completa.

    python meta_posts.py <url> <url> <url>
    python meta_posts.py --csv posts_meta.csv <url> <url>

Imprime una tabla ordenada por `taken_at`. No escribe nada en data/.
"""
import argparse
import csv

from playwright.sync_api import sync_playwright

import scraper_comentarios_ig as ig

CAMPOS = ["shortcode", "cuenta", "taken_at", "post_like_count", "post_comment_count",
          "caption", "url"]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+", help="URLs de los posts (/p/... o /reel/...)")
    ap.add_argument("--csv", help="además de imprimir, guarda la tabla en este CSV")
    ap.add_argument("--headful", action="store_true",
                    help="Muestra la ventana del navegador (para debuggear)")
    args = ap.parse_args()

    posts = []
    for url in args.urls:
        sc = ig.shortcode_de_url(url)
        if not sc:
            ap.error(f"No pude sacar el shortcode de {url}")
        posts.append({"shortcode": sc, "media_id": str(ig.shortcode_a_media_id(sc)),
                      "cuenta": None, "caption": None})

    cookies = ig.cargar_sesion()
    filas = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        context = ig.crear_contexto_navegador(browser, cookies)
        page = context.new_page()
        try:
            for i, post in enumerate(posts):
                print(f"[{i+1}/{len(posts)}] {post['shortcode']} ...", flush=True)
                page.goto(ig.construir_url_post(post), wait_until="networkidle", timeout=60000)
                ig._detectar_bloqueo_url(page)
                page.wait_for_timeout(1500)
                meta = ig.extraer_meta_post(page, post)
                filas.append({**post, **meta})
                if i + 1 < len(posts):
                    ig.delay((3.0, 7.0))
        finally:
            browser.close()

    filas.sort(key=lambda f: (f["taken_at"] or "", int(f["media_id"])))

    print("\n| # | shortcode | cuenta | taken_at | likes | comentarios |")
    print("|---:|---|---|---|---:|---:|")
    for i, f in enumerate(filas):
        print(f"| {i} | {f['shortcode']} | {f['cuenta']} | {f['taken_at']} | "
              f"{f['post_like_count']} | {f['post_comment_count']} |")

    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CAMPOS, extrasaction="ignore")
            w.writeheader()
            w.writerows(filas)
        print(f"\nGuardado en {args.csv}")


if __name__ == "__main__":
    main()
