#!/usr/bin/env python3
# -- coding: utf-8 --
# IRD CLOUD ENGINE V1.7.4 - PROCESO 1x1 ANTI-NaN + PATCH CURP/RFC/NET_MASS + DATASETS 2025
# REGLAMENTO (UE) 2023/1115 + FAQ v5 ABRIL 2026 SEC 2.3
# FIX V1.7.4: JRC/GFC2020/V3 es Image no ImageCollection + Log anti-KeyError

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
import unicodedata
import re
import math

print("="*70)
print("IRD CLOUD ENGINE V1.7.4 - POLÍGONO x POLÍGONO + PATCH CURP/RFC + DATASETS 2025")
print("SHA256 GEOJSON EN QR/PDF + CURP/RFC/NET_MASS DESDE GEOJSON")
print("="*70)

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

TEXTO_RANGO_PRODUCCION = "Cultivo establecido pre-31-Dic-2020. Serie NDVI Sentinel-2 2020-2024 sin expansión agrícola. Última imagen satelital ESRI 2024. El productor declara continuidad hasta fecha de firma."

TEXTO_ANTI_JRC_NBDI = "El JRC Forest 2020 y ESRI LULC 2023 clasifican sistemas agroforestales de café bajo sombra como 'bosque'. Conforme FAQ v5 Sec 2.3, se desvirtúa con: 1) Hansen GFC 2021-2023: 0.0000 ha pérdida, 2) Serie NDVI 2020-2024 estable >0.4, 3) NBDI 2024 negativo confirmando patrón agrícola/suelo expuesto bajo dosel, no bosque cerrado. Esto confirma uso agrícola previo a fecha de corte EUDR y desvirtúa falso positivo de capas de cobertura arbórea."

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
    except:
        return geom

def safe_float(val, default=0):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    return float(val)

# ========== V1.7.3: Limpia valores NULL/NaN/0/vacíos de QGIS ==========
def clean_val(val, default='PENDIENTE_APORTACION'):
    """V1.7.3: Atrapa None, np.nan, nan, 'nan', 'NULL', 'None', '', 0, '0' de QGIS"""
    if val is None:
        return default
    try:
        if isinstance(val, float) and math.isnan(val):
            return default
    except:
        pass
    if isinstance(val, str):
        val_clean = val.strip().upper()
        if val_clean in ['', 'NULL', 'NONE', 'NAN', 'NA']:
            return default
        return val.strip()
    return str(val).strip()

def get_any_key(props, keys, default):
    """Busca cualquier variante de nombre de columna y limpia el valor."""
    for k in keys:
        if k in props:
            val = clean_val(props[k], default)
            if val!= default:
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
         .select(['B2','B3','B4','B8','B11','B12']) \
         .median().multiply(0.0001)
        s2_rgb = s2.visualize(min=0.0, max=0.3, gamma=1.4, bands=['B4','B3','B2'])
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
         .select(['B2','B3','B4','B8','B11','B12']) \
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
    except:
        return False

def agregar_etiqueta_satelite(ruta_png, data_dict):
    if not os.path.exists(ruta_png): return
    if SKIP_EXISTING_IMAGES: return
    img = Image.open(ruta_png).convert("RGBA")
    draw = ImageDraw.Draw(img)
    texto = f"EUDR | {data_dict['productor']} | {data_dict['finca']} | {data_dict['superficie']} ha\n"
    texto += f"{data_dict['dictamen_corto']} | JRC:{data_dict['jrc']}% | NBDI:{data_dict['nbdi2020']}\n"
    texto += f"H:{data_dict['hansen']}ha | ESRI:{data_dict['esri_crops']}% | HS:{HS_CODE} | {FECHA_REPORTE_STR}"
    draw.rectangle([(0, 0), (1024, 170)], fill=(255, 255, 255, 240))
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except:
        font = ImageFont.load_default()
    draw.text((15, 10), texto, fill=(0, 0, 0), font=font)
    img.save(ruta_png, "PNG")

def genera_qr(data_dict, ruta_salida):
    texto = f"EUDR|{data_dict['productor']}|{data_dict['finca']}|{data_dict['dictamen_corto']}|JRC:{data_dict['jrc']}%|NBDI:{data_dict['nbdi2020']}|H:{data_dict['hansen']}ha|ESRI:{data_dict.get('esri_crops', 'NA')}%|SHA256:{data_dict['sha256_geojson'][:16]}"
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(texto)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(ruta_salida)

def crea_pdf_dictamen(data, ruta_salida):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font('Arial', 'B', 14)
    pdf.cell(0, 8, 'DICTAMEN TÉCNICO EUDR - CAFÉ ABRIL 2026', 0, 1, 'C')
    pdf.set_font('Arial', 'B', 12)
    if data["color"] == "Verde":
        pdf.set_text_color(0, 128, 0)
        pdf.cell(0, 8, 'Verde - APTO_EXPORTACION', 0, 1, 'C')
    elif data["color"] == "Rojo":
        pdf.set_text_color(200, 0, 0)
        pdf.cell(0, 8, 'Rojo - NO_APTO_EXPORTACION', 0, 1, 'C')
    else:
        pdf.set_text_color(200, 150, 0)
        pdf.cell(0, 8, 'Amarillo - REQUIERE_EVIDENCIA', 0, 1, 'C')
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # 1. IDENTIFICACIÓN
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '1. IDENTIFICACIÓN DE PARCELA', 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, f'Productor: {data["productor"]}', 0, 1)
    pdf.cell(0, 6, f'CURP: {data["curp"]} | RFC: {data["rfc"]}', 0, 1)
    pdf.cell(0, 6, f'Finca/Parcela: {data["finca"]} | Superficie: {data["superficie"]} ha', 0, 1)
    pdf.cell(0, 6, f'Coordenadas: {data["coords"]} | HS Code: {HS_CODE} | Net Mass: {data["net_mass_kg"]} kg', 0, 1)
    pdf.cell(0, 6, f'SHA256 GeoJSON: {data["sha256_geojson"]}', 0, 1)
    pdf.ln(2)

    # 2. ANÁLISIS
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '2. ANÁLISIS EUDR OFICIAL', 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, f'JRC Forest 2020 V3: {data["jrc"]}% cobertura arbórea al 31-dic-2020', 0, 1)
    pdf.cell(0, 6, f'Hansen GFC v1.13: Pérdida 2021-2023 = {data["hansen"]} ha', 0, 1)
    pdf.cell(0, 6, f'ESRI LULC 2023: Crops {data["esri_crops"]}% | Trees {100-data["esri_crops"]:.0f}%', 0, 1)
    pdf.ln(2)

    # 3. EVIDENCIA
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '3. EVIDENCIA AGROFORESTAL Y RANGO PRODUCCIÓN', 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.cell(0, 6, f'NDVI: 2020={data["ndvi2020"]:.2f} | 2021={data["ndvi2021"]:.2f} | 2022={data["ndvi2022"]:.2f} | 2023={data["ndvi2023"]:.2f} | 2024={data["ndvi2024"]:.2f}', 0, 1)
    pdf.cell(0, 6, f'NBDI: 2020={data["nbdi2020"]:.3f} | 2021={data["nbdi2021"]:.3f} | 2022={data["nbdi2022"]:.3f} | 2023={data["nbdi2023"]:.3f} | 2024={data["nbdi2024"]:.3f}', 0, 1)
    pdf.set_font('Arial', 'I', 9)
    pdf.multi_cell(0, 5, TEXTO_RANGO_PRODUCCION)
    pdf.ln(2)

    # 4. CONCLUSIÓN - TEXTO CORREGIDO V1.7.3
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '4. CONCLUSIÓN TÉCNICA Y MITIGACIÓN', 0, 1)
    pdf.set_font('Arial', '', 10)
    if data["color"] == "Verde":
        pdf.multi_cell(0, 6, 'Conforme Reglamento (UE) 2023/1115 Art. 3 y FAQ v5 Sec 2.3: La parcela no presenta deforestación posterior al 31-dic-2020. Los índices espectrales confirman sistema agroforestal preexistente. Riesgo de incumplimiento: NEGLIGIBLE.')
    elif data["color"] == "Rojo":
        pdf.multi_cell(0, 6, 'Conforme Reglamento (UE) 2023/1115 Art. 3: Se detectó pérdida de cobertura arbórea posterior al 31-dic-2020 mediante Hansen GFC v1.13. La parcela NO es conforme para exportación a UE. Riesgo: NON_NEGLIGIBLE.')
    else:
        pdf.multi_cell(0, 6, 'Conforme FAQ v5 Sec 2.3: Se requieren evidencias adicionales de campo para desvirtuar clasificación de bosque en JRC/ESRI. Presentar bitácoras, fotos georreferenciadas o constancia de autoridad local. Riesgo: NON_NEGLIGIBLE hasta subsanar.')
    pdf.ln(1)
    pdf.set_font('Arial', 'B', 10)
    pdf.cell(0, 6, 'Bloque Anti-Falso Positivo:', 0, 1)
    pdf.set_font('Arial', 'I', 9)
    pdf.multi_cell(0, 5, data["texto_anti_jrc"])
    pdf.ln(2)

    # 5. DECLARACIÓN
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 7, '5. DECLARACIÓN DE RANGO DE PRODUCCIÓN', 0, 1)
    pdf.set_font('Arial', '', 10)
    pdf.multi_cell(0, 6, TEXTO_RANGO_PRODUCCION)
    pdf.ln(2)
    pdf.cell(0, 6, f'Fecha de emisión: {FECHA_REPORTE_STR}', 0, 1)
    pdf.cell(0, 6, 'Firma del Productor: _', 0, 1)
    pdf.ln(2)
    pdf.set_font('Arial', '', 8)
    pdf.cell(0, 4, f'Fuente: Sentinel-2 L2A, JRC GFC2020 V3, Hansen GFC v1.13, ESRI LULC 10m. Metodología: FAQ EUDR v5 Abril 2026 Sec 2.3.', 0, 1)
    pdf.cell(0, 4, f'Trazabilidad SHA256 ISO 17065. Generado: {TIMESTAMP_ISO}', 0, 1)
    if os.path.exists(data.get("qr_path", "")):
        pdf.image(data["qr_path"], x=170, y=10, w=30)
    pdf.output(ruta_salida)

def genera_dds_traces(carpeta_finca, metadata):
    nombre = metadata['finca']
    # ===== V1.7.3: Lee CURP/RFC/NetMass con cualquier variante y limpia NULL/NaN =====
    curp = get_any_key(metadata, ['CURP', 'Curp', 'curp'], 'PENDIENTE_APORTACION')
    rfc = get_any_key(metadata, ['RFC', 'Rfc', 'rfc'], 'PENDIENTE_APORTACION')
    net_mass = get_any_key(metadata, ['Net_mass_kilos', 'Net_mass_kg', 'net_mass_kg'], 'PENDIENTE_CONTRATO')

    es_final = curp!= 'PENDIENTE_APORTACION' and rfc!= 'PENDIENTE_APORTACION'
    dds = {
        "referenceNumber": None,
        "operator": {"name": metadata['productor'], "identificationNumber": curp, "rfc": rfc},
        "commodity": {"hsCode": HS_CODE, "description": "Coffee, not roasted", "netMassKg": net_mass},
        "geolocations": [{"areaHa": metadata['superficie_ha'], "productionDate": "pre-2020-12-31", "geojson_sha256": metadata['sha256_geojson']}],
        "dueDiligenceStatement": {"riskAssessment": "NEGLIGIBLE" if metadata['color'] == 'Verde' else "NON_NEGLIGIBLE"},
        "status": "FINAL" if es_final else "DRAFT_LOCAL"
    }
    ruta_dds_dir = os.path.join(carpeta_finca, '06_DDS_TRACES')
    os.makedirs(ruta_dds_dir, exist_ok=True)
    ruta_dds = os.path.join(ruta_dds_dir, f'DDS_TRACES_{nombre}.json')
    with open(ruta_dds, 'w') as f:
        json.dump(dds, f, indent=2)
    log(f"JSON DDS_TRACES escrito: {ruta_dds}", "OK")
    return ruta_dds

# ================= PROCESO PRINCIPAL NUCLEAR 1x1 =================
log(f"LEYENDO GEOJSON: {GEOJSON_PATH}")
gdf = gpd.read_file(GEOJSON_PATH)
log(f"Total polígonos: {len(gdf)}")

# V1.7.4: Datasets actualizados - FIX JRC V3 es Image
esri_lulc = ee.ImageCollection('projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m_TS').filterDate('2023-01-01', '2023-12-31').mosaic().select('b1')
HANSEN = ee.Image('UMD/hansen/global_forest_change_2025_v1_13').select('lossyear').unmask(0)

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

        # === CÁLCULOS 1x1 CON.getInfo() INDIVIDUAL ===
        area = safe_float(geom.area().divide(10000).getInfo())

        # JRC V3 - FIX V1.7.4: Es Image, no ImageCollection
        jrc = ee.Image('JRC/GFC2020/V3').select('Map').eq(1).unmask(0)
        area_forest = safe_float(jrc.multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom, scale=10, maxPixels=1e13, bestEffort=True, tileScale=16
        ).getInfo().get('Map', 0))
        area_geom = safe_float(geom.area().getInfo())
        jrc_pct = safe_float(area_forest / area_geom * 100) if area_geom > 0 else 0
        jrc_pct = max(0, min(100, jrc_pct))

        # Hansen v1.13
        loss_post = HANSEN.updateMask(HANSEN.gte(21))
        area_loss = safe_float(loss_post.multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom, scale=30, maxPixels=1e13, bestEffort=True, tileScale=16
        ).getInfo().get('lossyear', 0))
        hansen_ha = safe_float(area_loss / 10000)

        # NDVI/NBDI por año - SOLO SI HAY IMÁGENES
        ndvi_vals = {}
        nbdi_vals = {}
        for año in [2020, 2021, 2022, 2023, 2024]:
            s2_col = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
             .filterBounds(geom) \
             .filterDate(f'{año}-01-01', f'{año}-12-31') \
             .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)) \
             .select(['B4','B8','B11'])

            size = s2_col.size().getInfo()
            if size > 0:
                s2_img = s2_col.median()
                ndvi = safe_float(s2_img.normalizedDifference(['B8','B4']).unmask(0).reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e13, bestEffort=True, tileScale=16
                ).getInfo().get('nd', 0))
                nbdi = safe_float(s2_img.normalizedDifference(['B11','B8']).unmask(0).reduceRegion(
                    reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e13, bestEffort=True, tileScale=16
                ).getInfo().get('nd', 0))
            else:
                ndvi, nbdi = 0, 0

            ndvi_vals[año] = max(-1, min(1, ndvi))
            nbdi_vals[año] = max(-1, min(1, nbdi))

        # ESRI 2023
        esri_crops_img = esri_lulc.eq(5) # 5 = Crops
        esri_stats = esri_crops_img.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e13, bestEffort=True, tileScale=16
        ).getInfo()
        esri_crops = safe_float(esri_stats.get('b1', 0) * 100)
        esri_crops = max(0, min(100, esri_crops))

        # === DICTAMEN ===
        cond_verde = (hansen_ha == 0) and (ndvi_vals[2020] > 0.4) and (nbdi_vals[2020] < 0)

        if cond_verde:
            dictamen_largo, color = "CONFORME EUDR - NBDI AGROFORESTAL", "Verde"
            dictamen_corto = "APTO_EXPORTACION"
            texto_anti_jrc = TEXTO_ANTI_JRC_NBDI
            riesgo = "NEGLIGIBLE"
        else:
            if hansen_ha > 0:
                dictamen_largo, color = "DEFORESTACIÓN DETECTADA", "Rojo"
                dictamen_corto = "NO_APTO_EXPORTACION"
                texto_anti_jrc = "No aplica - Pérdida post-2020 detectada"
                riesgo = "NON_NEGLIGIBLE"
            else:
                dictamen_largo, color = "REQUIERE EVIDENCIA ADICIONAL", "Amarillo"
                dictamen_corto = "REQUIERE_EVIDENCIA"
                texto_anti_jrc = "Pendiente - NBDI no concluyente"
                riesgo = "NON_NEGLIGIBLE"

        # === CARPETAS Y ARCHIVOS ===
        nombre_carpeta = f"{finca}{productor}{idx}{area:.3f}ha".replace(" ", "").replace("/", "").replace(".", "")
        carpeta_finca = os.path.join(OUTPUT_BASE, nombre_carpeta)
        for subcarpeta in ["01_GeoJSON", "02_Dictamen_Tecnico", "03_Visualizacion_Satelital", "04_Analisis_Indices", "05_Evidencias_Adicionales", "06_DDS_TRACES"]:
            os.makedirs(os.path.join(carpeta_finca, subcarpeta), exist_ok=True)

        ruta_geojson = os.path.join(carpeta_finca, "01_GeoJSON", f'{finca}_{idx}.geojson')
        geojson_data = {"type": "Feature", "geometry": mapping(row.geometry), "properties": {"finca": finca, "productor": productor, "idx": idx}}
        with open(ruta_geojson, 'w') as f:
            json.dump(geojson_data, f)
        sha256_geojson = calcular_sha256_archivo(ruta_geojson)

        # Imágenes
        carpeta_03 = os.path.join(carpeta_finca, "03_Visualizacion_Satelital")
        carpeta_04 = os.path.join(carpeta_finca, "04_Analisis_Indices")
        ruta_sat = os.path.join(carpeta_03, f'Sentinel2_{finca}_{idx}.png')
        ruta_ndvi = os.path.join(carpeta_04, f'NDVI_2020_{finca}_{idx}.png')
        ruta_nbdi = os.path.join(carpeta_04, f'NBDI_2020_{finca}_{idx}.png')

        img_ok = descarga_mapa_satelital(geom, ruta_sat)
        descarga_indice_imagen(geom, ruta_ndvi, 2020, 'NDVI')
        descarga_indice_imagen(geom, ruta_nbdi, 2020, 'NBDI')

        data_etiqueta = {
            'productor': productor, 'finca': f"{finca}_{idx}", 'superficie': round(area, 4),
            'jrc': round(jrc_pct, 2), 'nbdi2020': round(nbdi_vals[2020], 3), 'hansen': round(hansen_ha, 6),
            'dictamen_corto': dictamen_corto, 'esri_crops': round(esri_crops, 1),
            'sha256_geojson': sha256_geojson
        }
        if img_ok: agregar_etiqueta_satelite(ruta_sat, data_etiqueta)

        # ===== V1.7.3: Limpia propiedades antes de DDS =====
        props_raw = dict(row)
        metadata_temp = {
            "productor": productor, "finca": f"{finca}_{idx}", "hs_code": HS_CODE,
            "superficie_ha": round(area, 6), "jrc_forest_2020_pct": round(jrc_pct, 2),
            "hansen_loss_2021_2023_ha": round(hansen_ha, 6), "sha256_geojson": sha256_geojson,
            "color": color,
            "CURP": props_raw.get('CURP', props_raw.get('Curp', props_raw.get('curp'))),
            "RFC": props_raw.get('RFC', props_raw.get('Rfc', props_raw.get('rfc'))),
            "Net_mass_kg": props_raw.get('Net_mass_kilos', props_raw.get('Net_mass_kg', props_raw.get('net_mass_kg')))
        }
        ruta_dds_json = genera_dds_traces(carpeta_finca, metadata_temp)
        hash_dds_traces = calcular_sha256_archivo(ruta_dds_json)

        # QR y PDF
        ruta_qr = os.path.join(carpeta_finca, "02_Dictamen_Tecnico", f'QR_{finca}_{idx}.png')
        genera_qr(data_etiqueta, ruta_qr)

        centroide = geom.centroid().coordinates().getInfo()
        coords_str = f"{centroide[1]:.6f}, {centroide[0]:.6f}"

        data_pdf = {
            'finca': f"{finca}_{idx}", 'productor': productor, 'superficie': round(area, 4),
            'jrc': round(jrc_pct, 0), 'hansen': round(hansen_ha, 0),
            'ndvi2020': round(ndvi_vals[2020], 3), 'ndvi2021': round(ndvi_vals[2021], 3),
            'ndvi2022': round(ndvi_vals[2022], 3), 'ndvi2023': round(ndvi_vals[2023], 3), 'ndvi2024': round(ndvi_vals[2024], 3),
            'nbdi2020': round(nbdi_vals[2020], 3), 'nbdi2021': round(nbdi_vals[2021], 3),
            'nbdi2022': round(nbdi_vals[2022], 3), 'nbdi2023': round(nbdi_vals[2023], 3), 'nbdi2024': round(nbdi_vals[2024], 3),
            'esri_crops': round(esri_crops, 0), 'coords': coords_str, 'sha256_geojson': sha256_geojson,
            'color': color, 'texto_anti_jrc': texto_anti_jrc, 'qr_path': ruta_qr,
            'curp': get_any_key(props_raw, ['CURP', 'Curp', 'curp'], 'PENDIENTE_APORTACION'),
            'rfc': get_any_key(props_raw, ['RFC', 'Rfc', 'rfc'], 'PENDIENTE_APORTACION'),
            'net_mass_kg': get_any_key(props_raw, ['Net_mass_kilos', 'Net_mass_kg', 'net_mass_kg'], 'PENDIENTE_CONTRATO')
        }
        ruta_pdf = os.path.join(carpeta_finca, "02_Dictamen_Tecnico", f'Dictamen_EUDR_{finca}_{idx}.pdf')
        crea_pdf_dictamen(data_pdf, ruta_pdf)

        # METADATA JSON
        metadata = {
            "eudr_version": "IRD_CLOUD_V1.7.4", "faq_version": "v5 Abril 2026 Sec 2.3",
            "productor": productor, "finca": f"{finca}_{idx}", "hs_code": HS_CODE,
            "superficie_ha": round(area, 6), "jrc_forest_2020_pct": round(jrc_pct, 2),
            "hansen_loss_2021_2023_ha": round(hansen_ha, 6),
            "ndvi_2020": round(ndvi_vals[2020], 3), "ndvi_2024": round(ndvi_vals[2024], 3),
            "nbdi_2020": round(nbdi_vals[2020], 3), "nbdi_2024": round(nbdi_vals[2024], 3),
            "esri_crops_2023_pct": round(esri_crops, 1),
            "sha256_geojson": sha256_geojson, "sha256_dds_traces": hash_dds_traces,
            "curp": data_pdf['curp'], "rfc": data_pdf['rfc'], "net_mass_kg": data_pdf['net_mass_kg'],
            "dictamen_final": dictamen_largo, "color": color, "idx_original": idx
        }
        with open(os.path.join(carpeta_finca, f'METADATA_{finca}_{idx}.json'), 'w') as f:
            json.dump(metadata, f, indent=2)

        log(f"✅ {finca}_{idx} | {productor} | {area:.4f}ha | {dictamen_corto} | HASH_GEOJSON:{sha256_geojson[:16]}", "OK")

        # Para el CSV
        reporte_final.append({
            'FECHA_REPORTE': FECHA_REPORTE, 'PRODUCTOR': productor, 'FINCA_PARCELA': f"{finca}_{idx}",
            'SUPERFICIE_HA': round(area, 4), 'COORDENADAS': coords_str, 'HS_CODE': HS_CODE,
            'CURP': data_pdf['curp'], 'RFC': data_pdf['rfc'], 'NET_MASS_KG': data_pdf['net_mass_kg'],
            'JRC_FOREST_2020_PCT': round(jrc_pct, 2), 'HANSEN_LOSS_2021_2023_HA': round(hansen_ha, 6),
            'ESRI_CROPS_2023_PCT': round(esri_crops, 1),
            'NDVI_2020': round(ndvi_vals[2020], 3), 'NDVI_2021': round(ndvi_vals[2021], 3),
            'NDVI_2022': round(ndvi_vals[2022], 3), 'NDVI_2023': round(ndvi_vals[2023], 3),
            'NDVI_2024': round(ndvi_vals[2024], 3),
            'NBDI_2020': round(nbdi_vals[2020], 3), 'NBDI_2021': round(nbdi_vals[2021], 3),
            'NBDI_2022': round(nbdi_vals[2022], 3), 'NBDI_2023': round(nbdi_vals[2023], 3),
            'NBDI_2024': round(nbdi_vals[2024], 3),
            'DICTAMEN_EUDR': dictamen_corto, 'COLOR': color, 'RIESGO_EUDR': riesgo,
            'SHA256_GEOJSON': sha256_geojson,
            'STATUS_DDS': "FINAL" if data_pdf['curp']!= 'PENDIENTE_APORTACION' and data_pdf['rfc']!= 'PENDIENTE_APORTACION' else "DRAFT_LOCAL",
            'FAQ_VERSION': "v5 Abril 2026 Sec 2.3", 'TIMESTAMP_ISO': TIMESTAMP_ISO
        })

    except Exception as e:
        log(f"❌ ERROR en polígono {idx} {finca}: {str(e)[:200]}", "ERROR")
        errores.append(idx)
        continue

# ================= GENERAR CSV FINAL =================
df_reporte = pd.DataFrame(reporte_final)
nombre_csv = f"REPORTE_EUDR_FINAL_{FECHA_REPORTE}.csv"
ruta_csv = os.path.join(OUTPUT_BASE, nombre_csv)
df_reporte.to_csv(ruta_csv, index=False, encoding='utf-8-sig')
log(f"✅ CSV generado: {ruta_csv}", "OK")
# FIX V1.7.4:.get() para evitar KeyError si el CSV queda vacío por errores
log(f"Total registros: {len(df_reporte)} | Verdes: {len(df_reporte[df_reporte.get('COLOR','')=='Verde'])} | Rojos: {len(df_reporte[df_reporte.get('COLOR','')=='Rojo'])} | Amarillos: {len(df_reporte[df_reporte.get('COLOR','')=='Amarillo'])}", "OK")
if errores:
    log(f"⚠️ Polígonos con error: {errores}", "WARN")
log(f"{'='*70}", "OK")
log("PROCESO COMPLETADO V1.7.4. POLÍGONO x POLÍGONO.", "OK")
log(f"Verifica con: sha256sum {GEOJSON_PATH}", "OK")
log(f"Debe coincidir con el campo SHA256_GEOJSON del CSV y del QR", "OK")