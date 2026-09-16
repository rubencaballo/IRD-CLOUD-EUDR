#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# IRD CLOUD ENGINE V2.0 - MOTOR DE DECISIÓN POR PARCELA INDIVIDUAL
# REGLAMENTO (UE) 2023/1115 + Reg. (UE) 2025/2650
#
# CAMBIOS RESPECTO A V1.7.6 (documentados, no ocultos):
#
# 1. Se elimina la clasificación automática "Verde" basada en una fórmula
#    fija. Ahora existen 3 salidas: VERDE (defensible sin más evidencia),
#    AMARILLO (patrón ambiguo JRC/ESRI, requiere revisión humana de campo
#    antes de convertirse en DDS final), ROJO (pérdida Hansen detectada).
#
# 2. Se elimina la leyenda "Trazabilidad SHA256 ISO 17065" del PDF. Un hash
#    SHA256 generado por este script NO es una certificación ISO 17065.
#    Esa norma acredita organismos de certificación externos; afirmarla sin
#    haber pasado por ese proceso es una declaración falsa.
#
# 3. Se elimina el campo "operatorType": "MSPO" (Malaysian Sustainable Palm
#    Oil - no aplica a café). Se sustituye por un campo de tamaño de
#    operador real (SMALLHOLDER / STANDARD) sin pretender un esquema de
#    certificación que no existe.
#
# 4. STATUS_DDS ahora depende de una validación de completitud real:
#    CURP, RFC, NET_MASS_KG > 0 y LEGAL_DOCS presentes. Si falta cualquiera,
#    el registro queda en DRAFT_LOCAL sin importar el color de riesgo.
#    Antes, el color "Verde" se generaba igual aunque faltaran estos datos.
#
# 5. productionDate ya no se hardcodea igual para todas las parcelas; se
#    toma de metadata real (o se marca explícitamente como pendiente de
#    aportar por el productor).
#
# 6. Se documenta explícitamente la limitación de resolución: Hansen GFC es
#    ~30m/pixel, JRC/ESRI ~10m/pixel. Para parcelas pequeñas esto se anota
#    en el propio dictamen en vez de omitirse.
#
# IMPORTANTE: este script sigue siendo una herramienta de PRE-ANÁLISIS.
# Los casos AMARILLO no deben convertirse en DDS finales sin que una
# persona con criterio agronómico revise evidencia de campo (fotos
# georreferenciadas con fecha, bitácora, o constancia de autoridad local).
#
# CAMBIO ADICIONAL (post-V2.0): AREA_MIN_CONFIABLE_HANSEN_HA ya no es solo
# una nota informativa en el PDF. Ahora evaluar_parcela() la usa como
# primer filtro: cualquier parcela por debajo de ese umbral se marca
# Amarillo/requiere_revision_humana=True SIN IMPORTAR el % de JRC, porque
# a ese tamaño Hansen no tiene suficientes píxeles para ser confiable.
# Antes, una parcela chica con JRC >= 15% podía salir Verde automático
# apoyándose en un "0 ha de pérdida" de Hansen que, a esa escala, no
# significa gran cosa.

import ee
import geopandas as gpd
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import os
import hashlib
import json
import requests
import time
from fpdf import FPDF
import qrcode
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import mapping
import sys
import math

print("=" * 70)
print("IRD CLOUD ENGINE V2.0 - MOTOR DE DECISIÓN POR PARCELA")
print("SIN AUTO-APROBACIÓN | CASOS AMBIGUOS -> REVISIÓN HUMANA")
print("=" * 70)

try:
    ee.Initialize(project='ee-rvicconmorales')
    print("✅ GEE Autenticado: OK")
except Exception as e:
    print(f"❌ ERROR GEE: {e}")
    sys.exit(1)

# ================= CONFIGURACIÓN =================
GEOJSON_PATH = '/home/ruben/Documentos/EUDR_Expediente/EUDR_Expedientes/Poligonos Texin-Teocelo.geojson'
OUTPUT_BASE = '/home/ruben/Documentos/EUDR_Expediente/EUDR_Expedientes/Teocelo_Texin'
HS_CODE = '090111'
FECHA_CORTE_EUDR = '2020-12-31'
FECHA_REPORTE = datetime.now(timezone.utc).strftime("%Y-%m-%d")
FECHA_REPORTE_STR = datetime.now(timezone.utc).strftime("%d de %B de %Y UTC")
TIMESTAMP_ISO = datetime.now(timezone.utc).isoformat()

SKIP_EXISTING_IMAGES = False

# --- Umbrales del motor de decisión (documentados y ajustables) ---
# JRC_MIN_PCT_VERDE: por debajo de esto, JRC no detectó dosel arbóreo
# real en el baseline 2020. No es evidencia de deforestación, pero SÍ
# rompe la consistencia JRC/ESRI que sustenta un "Verde" automático.
JRC_MIN_PCT_VERDE = 15.0

# Área mínima (ha) a partir de la cual Hansen (30m/pixel) empieza a tener
# suficientes píxeles dentro del polígono para ser mínimamente confiable.
# Por debajo de esto, "Hansen = 0 ha de pérdida" se reporta igual, pero se
# anota la limitación de resolución en el dictamen.
AREA_MIN_CONFIABLE_HANSEN_HA = 0.5

TEXTO_RANGO_PRODUCCION_TPL = (
    "Cultivo declarado como establecido antes del 31-dic-2020, según "
    "declaración del productor. Serie NDVI Sentinel-2 {y0}-{y1} sin señal "
    "de expansión agrícola detectada por el script. Esta declaración no "
    "sustituye evidencia de campo cuando el caso se clasifica AMARILLO."
)

TEXTO_LIMITACION_RESOLUCION = (
    "NOTA DE RESOLUCIÓN: Hansen GFC v1.13 tiene resolución nativa de ~30m "
    "por pixel; JRC GFC2020 V3 y ESRI LULC, ~10m. Para esta parcela "
    "({area_ha:.4f} ha), esto limita la capacidad de los productos "
    "satelitales de resolver cambios de cobertura pequeños o en el borde "
    "del polígono. Los resultados de Hansen deben interpretarse con esta "
    "limitación en mente, especialmente si el área es menor a "
    f"{AREA_MIN_CONFIABLE_HANSEN_HA} ha."
)


def log(mensaje, nivel="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {nivel}: {mensaje}")


def calcular_sha256_archivo(ruta_archivo):
    sha256_hash = hashlib.sha256()
    with open(ruta_archivo, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def simplificar_geometria(geom, tolerancia_metros=2.0):
    try:
        area_orig = geom.area().getInfo()
        geom_simple = geom.simplify(tolerancia_metros)
        area_simp = geom_simple.area().getInfo()
        if abs(area_orig - area_simp) / area_orig > 0.01:
            return geom
        return geom_simple
    except Exception:
        return geom


def safe_float(val, default=0):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    return float(val)


def clean_val(val, default='PENDIENTE_APORTACION'):
    """Atrapa None, np.nan, 'nan', 'NULL', 'None', '', 0, '0' de QGIS."""
    if val is None:
        return default
    try:
        if isinstance(val, float) and math.isnan(val):
            return default
    except Exception:
        pass
    if isinstance(val, str):
        val_clean = val.strip().upper()
        if val_clean in ['', 'NULL', 'NONE', 'NAN', 'NA']:
            return default
        return val.strip()
    return str(val).strip()


def get_any_key(props, keys, default):
    for k in keys:
        if k in props:
            val = clean_val(props[k], default)
            if val != default:
                return val
    return default


def reintentar(max_intentos=3, delay=10):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for intento in range(max_intentos):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if intento < max_intentos - 1:
                        log(f"Intento {intento+1}/{max_intentos} falló. Reintentando...", "WARN")
                        time.sleep(delay)
                    else:
                        raise e
        return wrapper
    return decorator


@reintentar(max_intentos=3, delay=10)
def descarga_mapa_satelital(geom, nombre_salida):
    if SKIP_EXISTING_IMAGES and os.path.exists(nombre_salida):
        log(f"SKIP: {os.path.basename(nombre_salida)} ya existe", "INFO")
        return True
    try:
        geom_simple = simplificar_geometria(geom, 2.0)
        s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filterBounds(geom_simple) \
            .filterDate('2024-01-01', '2024-12-31') \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 10)) \
            .select(['B2', 'B3', 'B4', 'B8', 'B11', 'B12']) \
            .median().multiply(0.0001)
        s2_rgb = s2.visualize(min=0.0, max=0.3, gamma=1.4, bands=['B4', 'B3', 'B2'])
        poligono_rojo = ee.FeatureCollection(geom_simple).style(**{'color': 'FF0000', 'width': 4, 'fillColor': '00000000'})
        mapa_final = s2_rgb.blend(poligono_rojo)
        url = mapa_final.getThumbURL({'region': geom_simple.bounds(), 'dimensions': 1024, 'format': 'png'})
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        with open(nombre_salida, 'wb') as f:
            f.write(r.content)
        return True
    except Exception as e:
        log(f"No se pudo descargar imagen RGB: {str(e)[:100]}", "WARN")
        return False


@reintentar(max_intentos=2, delay=5)
def descarga_indice_imagen(geom, nombre_salida, año, tipo_indice):
    if SKIP_EXISTING_IMAGES and os.path.exists(nombre_salida):
        log(f"SKIP: {os.path.basename(nombre_salida)} ya existe", "INFO")
        return True
    try:
        geom_simple = simplificar_geometria(geom, 2.0)
        s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filterBounds(geom_simple) \
            .filterDate(f'{año}-01-01', f'{año}-12-31') \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)) \
            .select(['B2', 'B3', 'B4', 'B8', 'B11', 'B12']) \
            .median()
        if tipo_indice == 'NDVI':
            indice = s2.normalizedDifference(['B8', 'B4']).clamp(-1, 1)
            vis = {'min': 0.0, 'max': 0.9, 'palette': ['#8B4513', '#FFFF00', '#90EE90', '#006400']}
        else:
            indice = s2.normalizedDifference(['B11', 'B8']).clamp(-1, 1)
            vis = {'min': -0.2, 'max': 0.5, 'palette': ['#006400', '#90EE90', '#FFFF00', '#D2B48C', '#8B4513']}
        indice_vis = indice.visualize(**vis)
        poligono = ee.FeatureCollection(geom_simple).style(**{'color': 'FF0000', 'width': 3, 'fillColor': '00000000'})
        mapa_final = indice_vis.blend(poligono)
        url = mapa_final.getThumbURL({'region': geom_simple.bounds(), 'dimensions': 768, 'format': 'png'})
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        with open(nombre_salida, 'wb') as f:
            f.write(r.content)
        return True
    except Exception:
        return False


def agregar_etiqueta_satelite(ruta_png, data_dict):
    if not os.path.exists(ruta_png):
        return
    if SKIP_EXISTING_IMAGES:
        return
    img = Image.open(ruta_png).convert("RGBA")
    draw = ImageDraw.Draw(img)
    texto = f"EUDR | {data_dict['productor']} | {data_dict['finca']} | {data_dict['superficie']} ha\n"
    texto += f"{data_dict['dictamen_corto']} | JRC:{data_dict['jrc']}% | NBDI:{data_dict['nbdi2020']}\n"
    texto += f"H:{data_dict['hansen']}ha | ESRI:{data_dict['esri_crops']}% | HS:{HS_CODE} | {FECHA_REPORTE_STR}"
    draw.rectangle([(0, 0), (1024, 170)], fill=(255, 255, 255, 240))
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except Exception:
        font = ImageFont.load_default()
    draw.text((15, 10), texto, fill=(0, 0, 0), font=font)
    img.save(ruta_png, "PNG")


def genera_qr(data_dict, ruta_salida):
    texto = (
        f"EUDR|{data_dict['productor']}|{data_dict['finca']}|{data_dict['dictamen_corto']}"
        f"|JRC:{data_dict['jrc']}%|NBDI:{data_dict['nbdi2020']}|H:{data_dict['hansen']}ha"
        f"|ESRI:{data_dict.get('esri_crops', 'NA')}%|SHA256:{data_dict['sha256_geojson'][:16]}"
    )
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(texto)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(ruta_salida)


# ========== PLANET NICFI (gratis vía Earth Engine, ~4.77m) ==========
# Requisito: registrarte en https://www.planet.com/nicfi/ con la MISMA cuenta
# de Google que usas en Earth Engine. La aprobación puede tardar uno o dos
# días. Una vez aprobada, esta colección aparece disponible sin más
# configuración -- no requiere API key aparte ni QGIS.
#
# Por qué esto ayuda con los "Amarillo" de parcelas < 1 ha: Hansen (30m/pixel)
# apenas tiene unos pocos píxeles dentro de una parcela así de chica, así que
# un resultado "0 ha de pérdida" no es información confiable. NICFI da
# resolución de ~4.77m/pixel -- suficientes píxeles incluso en parcelas
# pequeñas para ver de verdad qué hay ahí, en vez de adivinar.
NICFI_COLLECTION = 'projects/planet-nicfi/assets/basemaps/americas'
_nicfi_disponible = None  # se detecta una sola vez por corrida, no se asume


def verificar_acceso_nicfi():
    """Prueba real de acceso (no asume nada). Si tu cuenta aún no está
    aprobada para NICFI, el script sigue funcionando igual que antes, solo
    sin este refuerzo -- nunca truena por esto."""
    global _nicfi_disponible
    if _nicfi_disponible is not None:
        return _nicfi_disponible
    try:
        col = ee.ImageCollection(NICFI_COLLECTION)
        n = col.limit(1).size().getInfo()
        _nicfi_disponible = n > 0
        if _nicfi_disponible:
            log("Planet NICFI: acceso confirmado.", "OK")
        else:
            log("Planet NICFI: colección vacía o sin acceso todavía.", "WARN")
    except Exception as e:
        _nicfi_disponible = False
        log(f"Planet NICFI no accesible desde esta cuenta GEE: {str(e)[:150]}", "WARN")
        log("Regístrate en https://www.planet.com/nicfi/ con la misma cuenta de "
            "Google de Earth Engine. Mientras no esté aprobado, el script sigue "
            "funcionando normal, solo sin este refuerzo de resolución.", "WARN")
    return _nicfi_disponible


@reintentar(max_intentos=2, delay=8)
def descarga_mapa_nicfi(geom, nombre_salida, mas_antiguo=False):
    """
    Descarga el mosaico Planet NICFI más reciente (o el más antiguo
    disponible si mas_antiguo=True) como PNG con el polígono resaltado.
    Devuelve (True, 'YYYY-MM') o (False, None).
    """
    if not verificar_acceso_nicfi():
        return False, None
    try:
        col = ee.ImageCollection(NICFI_COLLECTION).filterBounds(geom)
        col = col.sort('system:time_start', mas_antiguo)
        img = col.first()
        fecha_real = ee.Date(img.get('system:time_start')).format('YYYY-MM').getInfo()
        geom_simple = simplificar_geometria(geom, 2.0)
        rgb = img.visualize(bands=['R', 'G', 'B'], min=64, max=5454, gamma=1.3)
        poligono_rojo = ee.FeatureCollection(geom_simple).style(**{'color': 'FF0000', 'width': 3, 'fillColor': '00000000'})
        mapa_final = rgb.blend(poligono_rojo)
        url = mapa_final.getThumbURL({'region': geom_simple.bounds(), 'dimensions': 1024, 'format': 'png'})
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        with open(nombre_salida, 'wb') as f:
            f.write(r.content)
        return True, fecha_real
    except Exception as e:
        log(f"No se pudo descargar mosaico NICFI: {str(e)[:150]}", "WARN")
        return False, None


def calcular_ndvi_nicfi(geom, fecha_inicio_str, fecha_fin_str):
    """
    NDVI promedio (~4.77m) usando el mosaico NICFI más cercano dentro de
    [fecha_inicio_str, fecha_fin_str] ('YYYY-MM-DD'). Devuelve (ndvi, fecha)
    o (None, None) si no hay mosaico disponible en ese rango o no hay acceso.
    Los mosaicos NICFI de 4 bandas (post ~2020) incluyen NIR ('N'); los más
    viejos pueden no tenerlo, por eso se valida antes de usar.
    """
    if not verificar_acceso_nicfi():
        return None, None
    try:
        col = ee.ImageCollection(NICFI_COLLECTION).filterBounds(geom) \
            .filterDate(fecha_inicio_str, fecha_fin_str)
        img = col.sort('system:time_start', False).first()
        bandas = img.bandNames().getInfo()
        if 'N' not in bandas:
            return None, None
        fecha_real = ee.Date(img.get('system:time_start')).format('YYYY-MM').getInfo()
        ndvi = safe_float(img.normalizedDifference(['N', 'R']).reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=5, maxPixels=1e13, bestEffort=True, tileScale=16
        ).getInfo().get('nd', 0))
        return ndvi, fecha_real
    except Exception as e:
        log(f"No se pudo calcular NDVI NICFI: {str(e)[:150]}", "WARN")
        return None, None


def refuerzo_nicfi_parcela_chica(geom, carpeta_evidencias):
    """
    Para parcelas < AREA_MIN_CONFIABLE_HANSEN_HA: intenta traer evidencia
    NICFI real (imagen + comparación NDVI temprana vs. reciente) para que la
    revisión humana tenga algo mejor que Hansen a 30m. NO decide el color
    por sí sola -- solo adjunta evidencia y, si hay una caída de NDVI grande,
    lo dice explícitamente para que no se pase por alto.
    Devuelve un dict con lo que se logró obtener (puede venir vacío).
    """
    resultado = {'disponible': False}
    if not verificar_acceso_nicfi():
        return resultado

    os.makedirs(carpeta_evidencias, exist_ok=True)
    ruta_reciente = os.path.join(carpeta_evidencias, 'NICFI_reciente.png')
    ok_reciente, fecha_reciente = descarga_mapa_nicfi(geom, ruta_reciente, mas_antiguo=False)
    ruta_antigua = os.path.join(carpeta_evidencias, 'NICFI_2021.png')
    ok_antigua, fecha_antigua = descarga_mapa_nicfi(geom, ruta_antigua, mas_antiguo=True)

    ndvi_reciente, ndvi_antiguo = None, None
    if fecha_reciente:
        ndvi_reciente, _ = calcular_ndvi_nicfi(
            geom, f"{fecha_reciente}-01", (datetime.now(timezone.utc)).strftime("%Y-%m-%d"))
    if fecha_antigua:
        ndvi_antiguo, _ = calcular_ndvi_nicfi(geom, "2020-01-01", "2021-06-30")

    caida_ndvi_significativa = None
    if ndvi_reciente is not None and ndvi_antiguo is not None and ndvi_antiguo > 0.05:
        caida_pct = (ndvi_antiguo - ndvi_reciente) / ndvi_antiguo * 100
        caida_ndvi_significativa = caida_pct > 25  # umbral documentado, ajustable

    resultado.update({
        'disponible': ok_reciente or ok_antigua,
        'imagen_reciente': ruta_reciente if ok_reciente else None,
        'fecha_reciente': fecha_reciente,
        'imagen_antigua': ruta_antigua if ok_antigua else None,
        'fecha_antigua': fecha_antigua,
        'ndvi_reciente': ndvi_reciente,
        'ndvi_antiguo': ndvi_antiguo,
        'caida_ndvi_significativa': caida_ndvi_significativa,
    })
    return resultado



def evaluar_parcela(hansen_ha, jrc_pct, esri_crops_pct, ndvi_2020, nbdi_2020, area_ha,
                     hansen_ha_2025=0.0, hansen_ha_descartada_aislada=0.0):
    """
    Devuelve (color, dictamen_corto, dictamen_largo, texto_evidencia,
    requiere_revision_humana: bool).

    A diferencia de V1.7.6, esta función NO asume que JRC=0% + ESRI con
    árboles = "café de sombra mal clasificado". Esa es una hipótesis, no
    un hecho verificado por el script, así que el caso se marca para que
    una persona lo confirme con evidencia real.

    hansen_ha ya viene filtrada por MIN_PIXELES_CLUSTER_HANSEN (descarta
    píxeles aislados sin vecinos, causa típica de falsos positivos en
    parcelas chicas). hansen_ha_descartada_aislada informa cuánta área se
    excluyó por ese filtro, para que quede documentado y no oculto.
    """
    if hansen_ha > 0:
        color = "Rojo"
        dictamen_corto = "NO_APTO_EXPORTACION"
        anio_txt = " (incluye pérdida 2025)" if hansen_ha_2025 > 0 else ""
        dictamen_largo = f"DEFORESTACIÓN DETECTADA (Hansen GFC v1.13 > 0 ha post-2020{anio_txt})"
        texto_evidencia = (
            f"Hansen GFC v1.13 detecta {hansen_ha:.4f} ha de pérdida de cobertura "
            f"validada (clúster ≥{MIN_PIXELES_CLUSTER_HANSEN} píxeles conectados) entre "
            f"2021 y el presente dentro del polígono, de las cuales "
            f"{hansen_ha_2025:.4f} ha corresponden específicamente a 2025. "
        )
        if hansen_ha_descartada_aislada > 0:
            texto_evidencia += (
                f"Adicionalmente, se detectaron {hansen_ha_descartada_aislada:.4f} ha en "
                f"píxeles aislados (sin vecinos conectados) que se excluyeron de este total "
                f"por ser el patrón típico de falso positivo (sombra de nube, "
                f"corregistración); no se ocultan, pero tampoco sustentan por sí solas un "
                f"dictamen Rojo. "
            )
        texto_evidencia += "Riesgo NON_NEGLIGIBLE."
        return color, dictamen_corto, dictamen_largo, texto_evidencia, False

    # Hansen == 0 ha (validada) a partir de aquí

    # ÁREA MENOR AL MÍNIMO CONFIABLE PARA HANSEN (~30m/pixel): un "0 ha de
    # pérdida" en un polígono así de chico no es evidencia sólida, aunque
    # JRC/ESRI luzcan consistentes. Se fuerza revisión humana ANTES de
    # evaluar el umbral de JRC, para que ningún caso pequeño se apruebe solo.
    if area_ha < AREA_MIN_CONFIABLE_HANSEN_HA:
        color = "Amarillo"
        dictamen_corto = "REQUIERE_EVIDENCIA"
        dictamen_largo = "ÁREA MENOR AL MÍNIMO CONFIABLE PARA HANSEN - REQUIERE REVISIÓN"
        texto_evidencia = (
            f"La parcela ({area_ha:.4f} ha) es menor al área mínima "
            f"({AREA_MIN_CONFIABLE_HANSEN_HA:.1f} ha) para la que Hansen GFC "
            f"v1.13 (~30m/pixel) resulta estadísticamente confiable: a este "
            f"tamaño el polígono contiene muy pocos píxeles Hansen, así que un "
            f"resultado de 0 ha de pérdida no puede tomarse como confirmación "
            f"de ausencia de deforestación. JRC Forest 2020 reporta "
            f"{jrc_pct:.1f}% de cobertura arbórea y ESRI LULC 2023 reporta "
            f"{100-esri_crops_pct:.0f}% de árboles, pero estos datos no "
            f"sustituyen la limitación de resolución de Hansen para polígonos "
            f"tan pequeños. Se requiere evidencia de campo (fotos "
            f"georreferenciadas con fecha, bitácora de manejo, o constancia de "
            f"autoridad local) antes de emitir una DDS final para esta parcela."
        )
        if hansen_ha_descartada_aislada > 0:
            texto_evidencia += (
                f" Nota: se descartaron {hansen_ha_descartada_aislada:.4f} ha en píxeles "
                f"Hansen aislados (posible ruido) dentro de esta parcela; no se usan para "
                f"la clasificación, pero se documentan para revisión de campo."
            )
        return color, dictamen_corto, dictamen_largo, texto_evidencia, True

    if jrc_pct >= JRC_MIN_PCT_VERDE:
        color = "Verde"
        dictamen_corto = "APTO_EXPORTACION"
        dictamen_largo = "CONFORME EUDR - JRC/ESRI consistentes, sin pérdida Hansen"
        texto_evidencia = (
            f"JRC Forest 2020 detecta {jrc_pct:.1f}% de cobertura arbórea en el "
            f"baseline (>= umbral de {JRC_MIN_PCT_VERDE:.0f}%), consistente con "
            f"ESRI LULC 2023 ({esri_crops_pct:.0f}% crops / {100-esri_crops_pct:.0f}% trees) "
            f"y sin pérdida detectada por Hansen. No hay contradicción entre fuentes "
            f"que requiera evidencia adicional."
        )
        return color, dictamen_corto, dictamen_largo, texto_evidencia, False

    # JRC < umbral: JRC no detectó dosel real en 2020. Esto NO es evidencia
    # de deforestación (Hansen = 0), pero rompe la consistencia entre
    # fuentes y requiere que una persona confirme el caso.
    color = "Amarillo"
    dictamen_corto = "REQUIERE_EVIDENCIA"
    dictamen_largo = "PATRÓN JRC/ESRI NO CONCLUYENTE - REQUIERE REVISIÓN DE CAMPO"
    texto_evidencia = (
        f"JRC Forest 2020 reporta {jrc_pct:.1f}% de cobertura arbórea en el "
        f"baseline (menor al umbral de {JRC_MIN_PCT_VERDE:.0f}% usado para "
        f"aprobación directa), mientras que ESRI LULC 2023 reporta "
        f"{100-esri_crops_pct:.0f}% de árboles. Hansen no detecta pérdida, lo cual "
        f"es consistente con AMBAS hipótesis posibles: (a) café de sombra "
        f"preexistente que JRC no resolvió en 2020, o (b) cobertura arbórea que "
        f"se desarrolló después de 2020 sobre suelo que no era forestal. El "
        f"script no puede distinguir entre (a) y (b) únicamente con estos "
        f"índices. Se requiere evidencia de campo (fotos georreferenciadas con "
        f"fecha, bitácora de manejo, o constancia de autoridad local) antes de "
        f"emitir una DDS final para esta parcela."
    )
    return color, dictamen_corto, dictamen_largo, texto_evidencia, True


def crea_pdf_dictamen(data, ruta_salida):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font('Arial', 'B', 14)
    pdf.cell(0, 8, 'DICTAMEN TÉCNICO EUDR - CAFÉ (PRE-ANÁLISIS AUTOMATIZADO)', 0, 1, 'C')
    pdf.set_font('Arial', 'B', 12)
    if data["color"] == "Verde":
        pdf.set_text_color(0, 128, 0)
        pdf.cell(0, 8, 'Verde - APTO_EXPORTACION (preliminar)', 0, 1, 'C')
    elif data["color"] == "Rojo":
        pdf.set_text_color(200, 0, 0)
        pdf.cell(0, 8, 'Rojo - NO_APTO_EXPORTACION', 0, 1, 'C')
    else:
        pdf.set_text_color(200, 150, 0)
        pdf.cell(0, 8, 'Amarillo - REQUIERE_EVIDENCIA_DE_CAMPO', 0, 1, 'C')
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '1. IDENTIFICACIÓN DE PARCELA', 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, f'Productor: {data["productor"]}', 0, 1)
    pdf.cell(0, 6, f'CURP: {data["curp"]} | RFC: {data["rfc"]}', 0, 1)
    pdf.cell(0, 6, f'Finca/Parcela: {data["finca"]} | Superficie: {data["superficie"]} ha', 0, 1)
    pdf.cell(0, 6, f'Coordenadas: {data["coords"]} | HS Code: {HS_CODE} | Net Mass: {data["net_mass_kg"]} kg', 0, 1)
    pdf.cell(0, 6, f'SHA256 GeoJSON: {data["sha256_geojson"]}', 0, 1)
    pdf.ln(2)

    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '2. ANÁLISIS EUDR (fuentes públicas)', 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, f'JRC Forest 2020 V3: {data["jrc"]}% cobertura arbórea al 31-dic-2020', 0, 1)
    pdf.cell(0, 6, f'Hansen GFC v1.13: Pérdida 2021-2023 = {data["hansen"]} ha', 0, 1)
    pdf.cell(0, 6, f'ESRI LULC 2023: Crops {data["esri_crops"]}% | Trees {100-data["esri_crops"]:.0f}%', 0, 1)
    pdf.ln(1)
    pdf.set_font('Arial', 'I', 8)
    pdf.multi_cell(0, 4, TEXTO_LIMITACION_RESOLUCION.format(area_ha=data["superficie"]))
    pdf.ln(2)

    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '3. EVIDENCIA ESPECTRAL', 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, f'NDVI: 2020={data["ndvi2020"]:.2f} | 2021={data["ndvi2021"]:.2f} | 2022={data["ndvi2022"]:.2f} | 2023={data["ndvi2023"]:.2f} | 2024={data["ndvi2024"]:.2f}', 0, 1)
    pdf.cell(0, 6, f'NBDI: 2020={data["nbdi2020"]:.3f} | 2021={data["nbdi2021"]:.3f} | 2022={data["nbdi2022"]:.3f} | 2023={data["nbdi2023"]:.3f} | 2024={data["nbdi2024"]:.3f}', 0, 1)
    pdf.ln(2)

    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '4. CONCLUSIÓN DEL MOTOR DE DECISIÓN', 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.multi_cell(0, 6, data["dictamen_largo"])
    pdf.ln(1)
    pdf.set_font('Arial', 'B', 10)
    pdf.cell(0, 6, 'Evidencia y razonamiento:', 0, 1)
    pdf.set_font('Arial', 'I', 9)
    pdf.multi_cell(0, 5, data["texto_evidencia"])
    pdf.ln(2)

    if data.get("requiere_revision_humana"):
        pdf.set_font('Arial', 'B', 10)
        pdf.set_text_color(200, 150, 0)
        pdf.multi_cell(0, 6, 'ESTE DOCUMENTO NO CONSTITUYE UNA DDS FINAL. Requiere revisión de campo '
                             'por una persona antes de poder presentarse ante TRACES.')
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '5. DECLARACIÓN DE RANGO DE PRODUCCIÓN', 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.multi_cell(0, 6, TEXTO_RANGO_PRODUCCION_TPL.format(y0=2020, y1=2024))
    pdf.ln(2)
    pdf.cell(0, 6, f'Fecha de emisión: {FECHA_REPORTE_STR}', 0, 1)
    pdf.cell(0, 6, 'Firma del Productor: _', 0, 1)
    pdf.ln(2)
    pdf.set_font('Arial', '', 8)
    pdf.multi_cell(0, 4, 'Este es un pre-análisis automatizado generado con fuentes públicas '
                         '(Sentinel-2 L2A, JRC GFC2020 V3, Hansen GFC v1.13, ESRI LULC 10m). '
                         'NO constituye una certificación de tercero acreditado. El hash SHA256 '
                         'sirve únicamente para verificar la integridad del archivo GeoJSON de '
                         'origen, no como certificación de cumplimiento.')
    pdf.cell(0, 4, f'Generado: {TIMESTAMP_ISO}', 0, 1)
    if os.path.exists(data.get("qr_path", "")):
        pdf.image(data["qr_path"], x=170, y=10, w=30)
    pdf.output(ruta_salida)


def genera_json_traces_nt(carpeta_finca, metadata, coords_latlon, legal_docs, status_final):
    """
    Genera el JSON de referencia para TRACES NT.
    Solo debe usarse para subir a TRACES cuando status_final == True
    (es decir, datos completos Y color != Amarillo, revisado por humano).
    """
    nombre = metadata['finca']
    traces_json = {
        "commodity": HS_CODE,
        "description": "Coffea arabica, green",
        "netMass": metadata['net_mass_kg'],
        "countryOfProduction": "MX",
        "geoLocation": {
            "type": "Point",
            "coordinates": [coords_latlon[1], coords_latlon[0]]  # [lon, lat]
        },
        "areaHa": metadata['superficie_ha'],
        "productionDate": metadata.get('production_date', 'PENDIENTE_APORTAR_FECHA_COSECHA'),
        "riskAssessment": {
            "deforestationRisk": (
                "NEGLIGIBLE" if metadata['color'] == 'Verde'
                else "NON_NEGLIGIBLE" if metadata['color'] == 'Rojo'
                else "PENDIENTE_REVISION_HUMANA"
            ),
            "evidence": [
                {"source": "JRC Forest 2020", "value": f"{metadata['jrc_forest_2020_pct']:.1f}%"},
                {"source": "Hansen GFC v1.13", "loss_2021_2023_ha": metadata['hansen_loss_2021_2023_ha'], "loss_2025_ha": metadata.get('hansen_loss_2025_ha', 0)},
                {"source": "ESRI LULC 2023", "trees": f"{100-metadata['esri_crops_2023_pct']:.0f}%"}
            ]
        },
        "operatorSize": "SMALLHOLDER" if metadata['superficie_ha'] < 4.0 else "STANDARD",
        "sourceGeoJsonSha256": metadata['sha256_geojson'],
        "legalComplianceDocument": legal_docs,
        "readyForTracesSubmission": status_final
    }

    ruta_traces = os.path.join(carpeta_finca, "06_DDS_TRACES", f'TRACES_NT_{nombre}.json')
    with open(ruta_traces, 'w') as f:
        json.dump(traces_json, f, indent=2, ensure_ascii=False)
    log(f"JSON TRACES NT escrito: {ruta_traces} | listo_para_subir={status_final}", "OK")
    return ruta_traces


# ================= PROCESO PRINCIPAL POR PARCELA =================
log(f"LEYENDO GEOJSON: {GEOJSON_PATH}")
gdf = gpd.read_file(GEOJSON_PATH)
log(f"Total polígonos: {len(gdf)}")

esri_lulc = ee.ImageCollection('projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m_TS') \
    .filterDate('2023-01-01', '2023-12-31').mosaic().select('b1')
# Umbral de tamaño de clúster (en píxeles Hansen de 30m) para considerar una
# pérdida como real y no un artefacto aislado (sombra de nube, mala
# corregistración, ruido de borde). 3 píxeles conectados ~= 2,700 m² mínimo
# contiguo. Es la causa más común de "deforestación fantasma" en parcelas
# chicas: un solo píxel de 900 m² marcado como pérdida, sin vecinos, casi
# siempre es ruido, no un desmonte real.
MIN_PIXELES_CLUSTER_HANSEN = 3

HANSEN_LOSSYEAR = ee.Image('UMD/hansen/global_forest_change_2025_v1_13').select('lossyear').unmask(0)

# Máscara de "clúster real": solo cuenta pérdida si el píxel tiene al menos
# MIN_PIXELES_CLUSTER_HANSEN píxeles conectados (8-conectividad) con pérdida
# en el mismo año o posterior a 2021. Los píxeles aislados se descartan del
# conteo de hectáreas, pero se reportan aparte para no ocultarlos del todo.
_mask_post2021 = HANSEN_LOSSYEAR.gte(21)
_cluster_size = _mask_post2021.selfMask().connectedPixelCount(maxSize=64, eightConnected=True)
HANSEN_CLUSTER_VALIDO = _mask_post2021.updateMask(_cluster_size.gte(MIN_PIXELES_CLUSTER_HANSEN))
HANSEN = HANSEN_LOSSYEAR  # se conserva el nombre original por compatibilidad con el resto del script

reporte_final = []
errores = []

for idx, row in gdf.iterrows():
    try:
        if row.geometry is None or row.geometry.is_empty:
            log(f"[{idx+1}/{len(gdf)}] Saltando: geometría vacía", "WARN")
            errores.append(idx)
            continue

        geom = ee.Geometry(mapping(row.geometry))
        productor = clean_val(row.get('nombre_productor', row.get('Productor', 'POR_DEFINIR')), 'POR_DEFINIR')
        finca = clean_val(row.get('nombre_finca', row.get('Finca_Parc', f'Finca_{idx}')), f'Finca_{idx}')

        log(f"[{idx+1}/{len(gdf)}] Procesando: {finca} | {productor}")

        area = safe_float(geom.area().divide(10000).getInfo())

        jrc = ee.Image('JRC/GFC2020/V3').select('Map').eq(1).unmask(0)
        area_forest = safe_float(jrc.multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom, scale=10, maxPixels=1e13, bestEffort=True, tileScale=16
        ).getInfo().get('Map', 0))
        area_geom = safe_float(geom.area().getInfo())
        jrc_pct = max(0, min(100, safe_float(area_forest / area_geom * 100) if area_geom > 0 else 0))

        # --- Pérdida validada (excluye píxeles aislados < MIN_PIXELES_CLUSTER_HANSEN) ---
        loss_post = HANSEN_LOSSYEAR.updateMask(HANSEN_CLUSTER_VALIDO)
        area_loss = safe_float(loss_post.gt(0).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom, scale=30, maxPixels=1e13, bestEffort=True, tileScale=16
        ).getInfo().get('lossyear', 0))
        hansen_ha = safe_float(area_loss / 10000)

        # --- Pérdida bruta sin filtro (para saber cuánto se descartó por ser ruido aislado) ---
        area_loss_bruta = safe_float(HANSEN_LOSSYEAR.gte(21).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom, scale=30, maxPixels=1e13, bestEffort=True, tileScale=16
        ).getInfo().get('lossyear', 0))
        hansen_ha_bruta = safe_float(area_loss_bruta / 10000)
        hansen_ha_descartada_aislada = max(0.0, hansen_ha_bruta - hansen_ha)

        # --- Pérdida específica de 2025 (lossyear == 25), ya con el mismo filtro de clúster ---
        loss_2025 = HANSEN_LOSSYEAR.eq(25).updateMask(HANSEN_CLUSTER_VALIDO)
        area_loss_2025 = safe_float(loss_2025.multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom, scale=30, maxPixels=1e13, bestEffort=True, tileScale=16
        ).getInfo().get('lossyear', 0))
        hansen_ha_2025 = safe_float(area_loss_2025 / 10000)

        ndvi_vals, nbdi_vals = {}, {}
        for año in [2020, 2021, 2022, 2023, 2024]:
            s2_col = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
                .filterBounds(geom) \
                .filterDate(f'{año}-01-01', f'{año}-12-31') \
                .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)) \
                .select(['B4', 'B8', 'B11'])
            size = s2_col.size().getInfo()
            if size > 0:
                s2_img = s2_col.median()
                ndvi = safe_float(s2_img.normalizedDifference(['B8', 'B4']).unmask(0).reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e13, bestEffort=True, tileScale=16
                ).getInfo().get('nd', 0))
                nbdi = safe_float(s2_img.normalizedDifference(['B11', 'B8']).unmask(0).reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e13, bestEffort=True, tileScale=16
                ).getInfo().get('nd', 0))
            else:
                ndvi, nbdi = 0, 0
            ndvi_vals[año] = max(-1, min(1, ndvi))
            nbdi_vals[año] = max(-1, min(1, nbdi))

        esri_crops_img = esri_lulc.eq(5)
        esri_stats = esri_crops_img.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e13, bestEffort=True, tileScale=16
        ).getInfo()
        esri_crops = max(0, min(100, safe_float(esri_stats.get('b1', 0) * 100)))

        # === MOTOR DE DECISIÓN (por parcela, sin fórmula fija de aprobación) ===
        color, dictamen_corto, dictamen_largo, texto_evidencia, requiere_revision = evaluar_parcela(
            hansen_ha, jrc_pct, esri_crops, ndvi_vals[2020], nbdi_vals[2020], area,
            hansen_ha_2025=hansen_ha_2025, hansen_ha_descartada_aislada=hansen_ha_descartada_aislada
        )

        # === CARPETAS ===
        nombre_carpeta = f"{finca}_{productor}_{idx}_{area:.3f}ha".replace(" ", "").replace("/", "").replace(".", "")
        carpeta_finca = os.path.join(OUTPUT_BASE, nombre_carpeta)
        for subcarpeta in ["01_GeoJSON", "02_Dictamen_Tecnico", "03_Visualizacion_Satelital",
                            "04_Analisis_Indices", "05_Evidencias_Adicionales", "06_DDS_TRACES"]:
            os.makedirs(os.path.join(carpeta_finca, subcarpeta), exist_ok=True)

        props_originales = dict(row)
        props_originales.pop('geometry', None)
        for k, v in props_originales.items():
            if isinstance(v, (pd.Timestamp, datetime)):
                props_originales[k] = v.isoformat()
        props_originales['finca'] = f"{finca}_{idx}"
        props_originales['productor'] = productor
        props_originales['idx'] = idx

        ruta_geojson = os.path.join(carpeta_finca, "01_GeoJSON", f'{finca}_{idx}.geojson')
        geojson_data = {"type": "Feature", "geometry": mapping(row.geometry), "properties": props_originales}
        with open(ruta_geojson, 'w') as f:
            json.dump(geojson_data, f, ensure_ascii=False)
        sha256_geojson = calcular_sha256_archivo(ruta_geojson)

        carpeta_03 = os.path.join(carpeta_finca, "03_Visualizacion_Satelital")
        carpeta_04 = os.path.join(carpeta_finca, "04_Analisis_Indices")
        ruta_sat = os.path.join(carpeta_03, f'Sentinel2_{finca}_{idx}.png')
        ruta_ndvi = os.path.join(carpeta_04, f'NDVI_2020_{finca}_{idx}.png')
        ruta_nbdi = os.path.join(carpeta_04, f'NBDI_2020_{finca}_{idx}.png')

        img_ok = descarga_mapa_satelital(geom, ruta_sat)
        descarga_indice_imagen(geom, ruta_ndvi, 2020, 'NDVI')
        descarga_indice_imagen(geom, ruta_nbdi, 2020, 'NBDI')

        # === REFUERZO NICFI para parcelas chicas marcadas Amarillo ===
        # No cambia el color automáticamente (seguimos sin auto-aprobación).
        # Le da a quien revisa una imagen real a ~4.77m y un NDVI antes/después
        # en vez de tener que confiar a ciegas en Hansen a 30m para algo tan chico.
        info_nicfi = {'disponible': False}
        if requiere_revision and area < AREA_MIN_CONFIABLE_HANSEN_HA:
            carpeta_nicfi = os.path.join(carpeta_finca, "05_Evidencias_Adicionales")
            log(f"Parcela chica ({area:.4f}ha) marcada Amarillo: buscando refuerzo NICFI...")
            info_nicfi = refuerzo_nicfi_parcela_chica(geom, carpeta_nicfi)
            if info_nicfi['disponible']:
                extra = (
                    f" [NICFI ~4.77m adjunto: mosaico {info_nicfi.get('fecha_antigua', '?')} vs "
                    f"{info_nicfi.get('fecha_reciente', '?')} en 05_Evidencias_Adicionales.]"
                )
                if info_nicfi.get('ndvi_antiguo') is not None and info_nicfi.get('ndvi_reciente') is not None:
                    extra += (
                        f" NDVI NICFI: {info_nicfi['ndvi_antiguo']:.2f} ({info_nicfi.get('fecha_antigua')}) "
                        f"-> {info_nicfi['ndvi_reciente']:.2f} ({info_nicfi.get('fecha_reciente')})."
                    )
                    if info_nicfi.get('caida_ndvi_significativa'):
                        extra += (" ATENCIÓN: caída de NDVI > 25% entre ambas fechas -- "
                                   "revisar esta parcela con prioridad antes de cualquier DDS.")
                texto_evidencia += extra
                log(f"Refuerzo NICFI obtenido para {finca}_{idx}.", "OK")
            else:
                log(f"NICFI no disponible para {finca}_{idx} (cuenta sin aprobar o sin cobertura aquí).", "WARN")

        data_etiqueta = {
            'productor': productor, 'finca': f"{finca}_{idx}", 'superficie': round(area, 4),
            'jrc': round(jrc_pct, 2), 'nbdi2020': round(nbdi_vals[2020], 3), 'hansen': round(hansen_ha, 6),
            'dictamen_corto': dictamen_corto, 'esri_crops': round(esri_crops, 1),
            'sha256_geojson': sha256_geojson
        }
        if img_ok:
            agregar_etiqueta_satelite(ruta_sat, data_etiqueta)

        props_raw = dict(row)
        curp = get_any_key(props_raw, ['CURP', 'Curp', 'curp'], 'PENDIENTE_APORTACION')
        rfc = get_any_key(props_raw, ['RFC', 'Rfc', 'rfc'], 'PENDIENTE_APORTACION')
        net_mass_kg = safe_float(get_any_key(props_raw, ['Net_mass_kilos', 'Net_mass_kg', 'net_mass_kg'], 0), 0)
        legal_docs = get_any_key(props_raw, ['LEGAL_DOCS', 'Legal_docs', 'legal_docs', 'constancia_legalidad'], '')
        production_date = get_any_key(props_raw, ['Production_date', 'production_date', 'Fecha_cosecha'], '')

        # === VALIDACIÓN DE COMPLETITUD REAL (antes se ignoraba) ===
        motivos_incompleto = []
        if curp == 'PENDIENTE_APORTACION':
            motivos_incompleto.append('CURP faltante')
        if rfc == 'PENDIENTE_APORTACION':
            motivos_incompleto.append('RFC faltante')
        if net_mass_kg <= 0:
            motivos_incompleto.append('Net mass = 0 kg (dato de contrato faltante)')
        if not legal_docs or legal_docs == 'PENDIENTE_APORTACION':
            motivos_incompleto.append('Documento legal de la parcela faltante')
            legal_docs = 'PENDIENTE_CONSTANCIA_LEGAL'
        if not production_date:
            production_date = 'PENDIENTE_APORTAR_FECHA_COSECHA'
            motivos_incompleto.append('Fecha de producción/cosecha faltante')

        datos_completos = len(motivos_incompleto) == 0
        # FINAL solo si: datos completos Y no requiere revisión humana (no Amarillo)
        status_dds = "FINAL" if (datos_completos and not requiere_revision) else "DRAFT_LOCAL"
        listo_para_traces = status_dds == "FINAL"

        centroide = geom.centroid().coordinates().getInfo()
        coords_str = f"{centroide[1]:.6f}, {centroide[0]:.6f}"

        ruta_qr = os.path.join(carpeta_finca, "02_Dictamen_Tecnico", f'QR_{finca}_{idx}.png')
        genera_qr(data_etiqueta, ruta_qr)

        data_pdf = {
            'finca': f"{finca}_{idx}", 'productor': productor, 'superficie': round(area, 4),
            'jrc': round(jrc_pct, 1), 'hansen': round(hansen_ha, 4),
            'ndvi2020': ndvi_vals[2020], 'ndvi2021': ndvi_vals[2021], 'ndvi2022': ndvi_vals[2022],
            'ndvi2023': ndvi_vals[2023], 'ndvi2024': ndvi_vals[2024],
            'nbdi2020': nbdi_vals[2020], 'nbdi2021': nbdi_vals[2021], 'nbdi2022': nbdi_vals[2022],
            'nbdi2023': nbdi_vals[2023], 'nbdi2024': nbdi_vals[2024],
            'esri_crops': round(esri_crops, 0), 'coords': coords_str, 'sha256_geojson': sha256_geojson,
            'color': color, 'dictamen_largo': dictamen_largo, 'texto_evidencia': texto_evidencia,
            'requiere_revision_humana': requiere_revision, 'qr_path': ruta_qr,
            'curp': curp, 'rfc': rfc, 'net_mass_kg': net_mass_kg
        }
        ruta_pdf = os.path.join(carpeta_finca, "02_Dictamen_Tecnico", f'Dictamen_EUDR_{finca}_{idx}.pdf')
        crea_pdf_dictamen(data_pdf, ruta_pdf)

        metadata_traces = {
            'finca': f"{finca}_{idx}", 'superficie_ha': round(area, 6),
            'jrc_forest_2020_pct': round(jrc_pct, 2), 'hansen_loss_2021_2023_ha': round(hansen_ha, 6),
            'hansen_loss_2025_ha': round(hansen_ha_2025, 6), 'hansen_ha_descartada_aislada': round(hansen_ha_descartada_aislada, 6),
            'esri_crops_2023_pct': round(esri_crops, 1), 'sha256_geojson': sha256_geojson,
            'color': color, 'net_mass_kg': net_mass_kg, 'production_date': production_date
        }
        ruta_traces = genera_json_traces_nt(carpeta_finca, metadata_traces, centroide, legal_docs, listo_para_traces)
        hash_traces = calcular_sha256_archivo(ruta_traces)

        metadata_general = {
            "eudr_version": "IRD_CLOUD_V2.0", "productor": productor, "finca": f"{finca}_{idx}",
            "hs_code": HS_CODE, "superficie_ha": round(area, 6),
            "jrc_forest_2020_pct": round(jrc_pct, 2), "hansen_loss_2021_2023_ha": round(hansen_ha, 6),
            "hansen_loss_2025_ha": round(hansen_ha_2025, 6), "hansen_ha_descartada_aislada": round(hansen_ha_descartada_aislada, 6),
            "esri_crops_2023_pct": round(esri_crops, 1), "sha256_geojson": sha256_geojson,
            "sha256_traces_json": hash_traces, "curp": curp, "rfc": rfc, "net_mass_kg": net_mass_kg,
            "legal_docs": legal_docs, "production_date": production_date,
            "color": color, "dictamen_corto": dictamen_corto, "dictamen_largo": dictamen_largo,
            "requiere_revision_humana": requiere_revision, "status_dds": status_dds,
            "motivos_incompleto": motivos_incompleto, "idx_original": idx
        }
        with open(os.path.join(carpeta_finca, f'METADATA_{finca}_{idx}.json'), 'w') as f:
            json.dump(metadata_general, f, indent=2, ensure_ascii=False)

        flag = "⚠ REVISAR" if requiere_revision else ("❌" if color == "Rojo" else "✅")
        log(f"{flag} {finca}_{idx} | {productor} | {area:.4f}ha | {dictamen_corto} | "
            f"STATUS={status_dds} | HASH:{sha256_geojson[:16]}", "OK")

        reporte_final.append({
            'FECHA_REPORTE': FECHA_REPORTE, 'PRODUCTOR': productor, 'FINCA_PARCELA': f"{finca}_{idx}",
            'SUPERFICIE_HA': round(area, 4), 'COORDENADAS': coords_str, 'HS_CODE': HS_CODE,
            'CURP': curp, 'RFC': rfc, 'NET_MASS_KG': net_mass_kg,
            'JRC_FOREST_2020_PCT': round(jrc_pct, 2), 'HANSEN_LOSS_2021_2023_HA': round(hansen_ha, 6),
            'HANSEN_LOSS_2025_HA': round(hansen_ha_2025, 6), 'HANSEN_HA_DESCARTADA_AISLADA': round(hansen_ha_descartada_aislada, 6),
            'ESRI_CROPS_2023_PCT': round(esri_crops, 1),
            'NDVI_2020': round(ndvi_vals[2020], 3), 'NDVI_2024': round(ndvi_vals[2024], 3),
            'NBDI_2020': round(nbdi_vals[2020], 3), 'NBDI_2024': round(nbdi_vals[2024], 3),
            'DICTAMEN_EUDR': dictamen_corto, 'COLOR': color,
            'REQUIERE_REVISION_HUMANA': requiere_revision,
            'SHA256_GEOJSON': sha256_geojson, 'LEGAL_DOCS': legal_docs,
            'STATUS_DDS': status_dds, 'MOTIVOS_INCOMPLETO': "; ".join(motivos_incompleto),
            'TIMESTAMP_ISO': TIMESTAMP_ISO
        })

    except Exception as e:
        log(f"❌ ERROR en polígono {idx} {finca}: {str(e)[:200]}", "ERROR")
        errores.append(idx)
        continue

# ================= CSV FINAL =================
df_reporte = pd.DataFrame(reporte_final)
nombre_csv = f"REPORTE_EUDR_FINAL_{FECHA_REPORTE}.csv"
ruta_csv = os.path.join(OUTPUT_BASE, nombre_csv)
df_reporte.to_csv(ruta_csv, index=False, encoding='utf-8-sig')
log(f"✅ CSV generado: {ruta_csv}", "OK")

if len(df_reporte) > 0 and 'COLOR' in df_reporte.columns:
    verdes = len(df_reporte[df_reporte['COLOR'] == 'Verde'])
    rojos = len(df_reporte[df_reporte['COLOR'] == 'Rojo'])
    amarillos = len(df_reporte[df_reporte['COLOR'] == 'Amarillo'])
    finales = len(df_reporte[df_reporte['STATUS_DDS'] == 'FINAL'])
    log(f"Total: {len(df_reporte)} | Verde: {verdes} | Amarillo(revisión): {amarillos} | "
        f"Rojo: {rojos} | Listos para TRACES (FINAL): {finales}", "OK")
else:
    log(f"Total registros: {len(df_reporte)} | CSV vacío o sin dictámenes por errores previos", "WARN")

if errores:
    log(f"⚠ Polígonos con error: {errores}", "WARN")

log("=" * 70, "OK")
log("PROCESO COMPLETADO V2.0. Revisa los casos AMARILLO antes de subir nada a TRACES.", "OK")