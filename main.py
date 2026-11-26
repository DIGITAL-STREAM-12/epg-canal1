from datetime import datetime, timedelta, timezone
import re
import sys
import unicodedata
from urllib.parse import urljoin
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import requests

URL_PROGRAMACION = "https://canal1cr.com/programacion/"
URL_GUIA_FALLBACK = "https://canal1cr.com/Guiatv/?action=public"
TZ_CR = timezone(timedelta(hours=-6))
CHANNEL_ID = "canal1.cr"
CHANNEL_NAME = "Canal 1 Costa Rica"
ARCHIVO_SALIDA = "epg_canal1_diario.xml"

DIAS_SEMANA = [
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
    "Domingo",
]


def normalizar_texto(texto: str) -> str:
    """Convierte a minúsculas, colapsa espacios y elimina tildes."""
    limpio = re.sub(r"\s+", " ", texto).strip().lower()
    return "".join(
        c
        for c in unicodedata.normalize("NFD", limpio)
        if unicodedata.category(c) != "Mn"
    )


def obtener_url_guia(session: requests.Session) -> str:
    """Obtiene la URL real del iframe de programación (cargado con lazy-load)."""
    try:
        resp = session.get(URL_PROGRAMACION, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for iframe in soup.find_all("iframe"):
            src = (
                iframe.get("data-litespeed-src")
                or iframe.get("data-src")
                or iframe.get("src")
                or ""
            ).strip()
            if src and src != "about:blank" and "guiatv" in src.lower():
                return urljoin(URL_PROGRAMACION, src)
    except requests.RequestException as err:
        print(f"[Aviso] No se pudo consultar {URL_PROGRAMACION}: {err}. Usando URL directa.")

    return URL_GUIA_FALLBACK


def parsear_hhmm(texto: str) -> tuple[int, int] | None:
    """Extrae (hora, minuto) en formato 24h desde una celda de hora."""
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", texto.strip())
    if not m:
        return None
    h, m_ = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= m_ <= 59:
        return (h, m_)
    return None


def obtener_parrilla_canal1() -> list[dict]:
    """Extrae la programación del día actual desde la guía HTML de Canal 1."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    }

    with requests.Session() as session:
        session.headers.update(headers)
        url_guia = obtener_url_guia(session)
        resp = session.get(url_guia, timeout=20)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    tabla = soup.find("table")
    if not tabla:
        print("[Error] No se encontró la tabla de programación.")
        return []

    encabezados = [
        normalizar_texto(th.get_text(" ", strip=True))
        for th in tabla.select("thead th")
    ]
    if not encabezados:
        primera_fila = tabla.find("tr")
        if primera_fila:
            encabezados = [
                normalizar_texto(c.get_text(" ", strip=True))
                for c in primera_fila.find_all(["th", "td"])
            ]

    hoy = datetime.now(TZ_CR).replace(hour=0, minute=0, second=0, microsecond=0)
    dia_actual = DIAS_SEMANA[hoy.weekday()]
    clave_dia = normalizar_texto(dia_actual)

    if clave_dia not in encabezados:
        print(f"[Error] No se encontró la columna '{dia_actual}' en {encabezados}.")
        return []

    idx_dia = encabezados.index(clave_dia)
    idx_ini = encabezados.index("inicio") if "inicio" in encabezados else 0
    idx_fin = encabezados.index("fin") if "fin" in encabezados else 1

    tbody = tabla.find("tbody") or tabla
    bloques = []

    for tr in tbody.find_all("tr"):
        celdas = tr.find_all("td")
        if len(celdas) <= max(idx_dia, idx_ini, idx_fin):
            continue

        ini_hm = parsear_hhmm(celdas[idx_ini].get_text(" ", strip=True))
        fin_hm = parsear_hhmm(celdas[idx_fin].get_text(" ", strip=True))
        if not ini_hm or not fin_hm:
            continue

        celda_prog = celdas[idx_dia]
        span_nombre = celda_prog.select_one(".program-name")
        if span_nombre:
            if "empty-name" in (span_nombre.get("class") or []):
                continue
            titulo = re.sub(r"\s+", " ", span_nombre.get_text(" ", strip=True)).strip()
        else:
            titulo = re.sub(r"\s+", " ", celda_prog.get_text(" ", strip=True)).strip()

        if not titulo or titulo in ("-", "—"):
            continue

        descripcion = ""
        p_desc = celda_prog.select_one(".tip p")
        if p_desc:
            for br in p_desc.find_all("br"):
                br.replace_with("\n")
            lineas_desc = [
                re.sub(r"[ \t]+", " ", l).strip()
                for l in p_desc.get_text().splitlines()
            ]
            descripcion = "\n".join(l for l in lineas_desc if l)

        icono = ""
        img_tip = celda_prog.select_one(".tip img")
        if img_tip and img_tip.get("src"):
            icono = urljoin(url_guia, img_tip["src"].strip())

        dt_inicio = hoy.replace(hour=ini_hm[0], minute=ini_hm[1])
        dt_fin = hoy.replace(hour=fin_hm[0], minute=fin_hm[1])
        if dt_fin <= dt_inicio:
            dt_fin += timedelta(days=1)

        # Unir franjas contiguas del mismo programa (ej. 01:00-02:00 + 02:00-03:00)
        if (
            bloques
            and bloques[-1]["dt_fin"] == dt_inicio
            and normalizar_texto(bloques[-1]["programa"]) == normalizar_texto(titulo)
        ):
            bloques[-1]["dt_fin"] = dt_fin
            if not bloques[-1]["descripcion"] and descripcion:
                bloques[-1]["descripcion"] = descripcion
            if not bloques[-1]["icono"] and icono:
                bloques[-1]["icono"] = icono
        else:
            bloques.append(
                {
                    "dt_inicio": dt_inicio,
                    "dt_fin": dt_fin,
                    "programa": titulo,
                    "descripcion": descripcion,
                    "icono": icono,
                }
            )

    return [
        {
            "channel_id": CHANNEL_ID,
            "programa": b["programa"],
            "descripcion": b["descripcion"],
            "icono": b["icono"],
            "inicio_xmltv": b["dt_inicio"].strftime("%Y%m%d%H%M%S %z"),
            "fin_xmltv": b["dt_fin"].strftime("%Y%m%d%H%M%S %z"),
        }
        for b in bloques
    ]


def guardar_xmltv(programas: list[dict], ruta_archivo: str = ARCHIVO_SALIDA) -> None:
    tv = ET.Element(
        "tv",
        {
            "generator-info-name": "Canal1CR-Daily-EPG",
            "source-info-url": URL_PROGRAMACION,
        },
    )

    canal = ET.SubElement(tv, "channel", {"id": CHANNEL_ID})
    display = ET.SubElement(canal, "display-name", {"lang": "es"})
    display.text = CHANNEL_NAME

    for item in programas:
        prog = ET.SubElement(
            tv,
            "programme",
            {
                "start": item["inicio_xmltv"],
                "stop": item["fin_xmltv"],
                "channel": item["channel_id"],
            },
        )
        title = ET.SubElement(prog, "title", {"lang": "es"})
        title.text = item["programa"]

        if item["descripcion"]:
            desc = ET.SubElement(prog, "desc", {"lang": "es"})
            desc.text = item["descripcion"]

        if item["icono"]:
            ET.SubElement(prog, "icon", {"src": item["icono"]})

    if hasattr(ET, "indent"):
        ET.indent(tv, space="  ")

    tree = ET.ElementTree(tv)
    tree.write(ruta_archivo, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    print("Obteniendo programación diaria de Canal 1 Costa Rica...")
    parrilla = obtener_parrilla_canal1()
    if not parrilla:
        print("No se pudo generar el EPG. Revisa la estructura de la web.")
        sys.exit(1)

    guardar_xmltv(parrilla, ARCHIVO_SALIDA)
    print(f"EPG generado con {len(parrilla)} programas en '{ARCHIVO_SALIDA}'.")
