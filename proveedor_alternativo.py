import os
import re
import json
import base64
import time
import unicodedata
import requests
import cv2
from PIL import Image

RUTA_CONFIG_IA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_ia.json")

MODELOS_GROQ_DISPONIBLES = {
    "OpenAI GPT-OSS 120B (Máximo Razonamiento Profundo - Recomendado)": "openai/gpt-oss-120b",
    "OpenAI GPT-OSS 20B (Razonamiento Ultrarrápido)": "openai/gpt-oss-20b",
    "Qwen 3.8 27B (Especialista en Código y QA)": "qwen/qwen3.8-27b",
    "Groq Compound (Arquitectura GPT-4 Multitarea)": "groq/compound"
}

ENDPOINT_GROQ = "https://api.groq.com/openai/v1/chat/completions"
ENDPOINT_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

CONFIG_DEFAULT = {
    "proveedor_activo": "deepseek_groq",  # "gemini" o "deepseek_groq"
    "groq_api_key": "",
    "openrouter_api_key": "",
    "modelo_groq": "openai/gpt-oss-120b"
}


def cargar_configuracion_ia() -> dict:
    if os.path.isfile(RUTA_CONFIG_IA):
        try:
            with open(RUTA_CONFIG_IA, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**CONFIG_DEFAULT, **data}
        except Exception:
            pass
    return dict(CONFIG_DEFAULT)


def guardar_configuracion_ia(cfg: dict):
    try:
        with open(RUTA_CONFIG_IA, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def extraer_fotogramas_video(ruta_video: str, max_frames: int = 5) -> list:
    """Extrae fotogramas clave uniformemente distribuidos a lo largo del video y los devuelve en base64 JPEG."""
    if not os.path.isfile(ruta_video):
        return []

    cap = cv2.VideoCapture(ruta_video)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        cap.release()
        return []

    # Distribuir índices uniformemente entre 10% y 90% de la duración
    fracciones = [0.1, 0.3, 0.5, 0.7, 0.9][:max_frames]
    indices = [max(0, min(total_frames - 1, int(total_frames * f))) for f in fracciones]
    indices = sorted(list(set(indices)))

    frames_b64 = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if ret and frame is not None:
            h, w = frame.shape[:2]
            if w > 1024:
                scale = 1024 / float(w)
                frame = cv2.resize(frame, (1024, int(h * scale)))
            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
            b64 = base64.b64encode(buf).decode('utf-8')
            frames_b64.append(b64)
    cap.release()
    return frames_b64


def codificar_imagen_base64(ruta_imagen: str) -> str:
    """Codifica y optimiza una imagen estática en base64 JPEG."""
    try:
        with Image.open(ruta_imagen) as img:
            img = img.convert("RGB")
            w, h = img.size
            if w > 1280:
                scale = 1280 / float(w)
                img = img.resize((1280, int(h * scale)), Image.Resampling.LANCZOS)
            import io
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception:
        with open(ruta_imagen, "rb") as f:
            return base64.b64encode(f.read()).decode('utf-8')


def ejecutar_llamada_openai_compatible(endpoint: str, api_key: str, payload: dict, timeout: float = 60.0) -> dict:
    """Ejecuta una petición REST hacia una API compatible con OpenAI (Groq, OpenRouter, DeepSeek)."""
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json"
    }
    if "openrouter.ai" in endpoint:
        headers["HTTP-Referer"] = "https://github.com/openthefly/qa-automation"
        headers["X-Title"] = "QA Automation Suite"

    res = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    if res.status_code != 200:
        raise RuntimeError(f"Error de API ({res.status_code}): {res.text}")
    return res.json()


MAPEO_MODELOS_ALIAS = {
    "deepseek-r1-distill-llama-70b": "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
    "llama-3.2-11b-vision-preview": "openai/gpt-oss-120b",
}


def extraer_cronologia_visual_video(archivo_path: str, api_key_gemini: str = None) -> str:
    """
    Utiliza EXCLUSIVAMENTE el motor de visión Gemini 3.6 Flash para inspeccionar el video
    segundo a segundo y extraer una bitácora visual cronológica exhaustiva.
    Restaura y conmuta automáticamente entre las 10 API Keys del pool ante cualquier límite 429.
    Si ninguna clave de Gemini 3.6 Flash puede analizar el video, cancela el proceso para evitar alucinaciones.
    """
    from google import genai
    from google.genai import types
    from gemini_keys_pool import gestor_pool, enmascarar_clave

    if not os.path.isfile(archivo_path):
        raise FileNotFoundError(f"El archivo de video no existe: {archivo_path}")

    total_claves = len(gestor_pool.datos.get("claves", [])) or 10
    cronologia = ""
    ultimo_err_vision = ""

    nombre_arch = os.path.basename(archivo_path)
    nombre_ascii = unicodedata.normalize('NFKD', nombre_arch).encode('ascii', 'ignore').decode('ascii')
    if not nombre_ascii.strip():
        nombre_ascii = "video_qa.mp4"

    prompt_vision = """Actúa como un extractor visual de precisión de video para QA en aplicaciones de punto de venta (POS).
Describe cronológicamente con marcas de tiempo (timestamp) cada acción, clic, botón presionado, texto digitado, PIN, montos numéricos, modales emergentes, códigos de error en pantalla y resultados observados en este video de un terminal POS On The Fly."""

    for intento_rotacion in range(total_claves):
        client, key_activa, num_clave = gestor_pool.obtener_cliente()
        gfile = None
        hubo_429 = False

        print(f"   [Visión IA - Gemini 3.6 Flash] Subiendo video con Clave #{num_clave}/{total_claves} ({enmascarar_clave(key_activa)})...")
        try:
            with open(archivo_path, "rb") as f:
                gfile = client.files.upload(
                    file=f,
                    config=types.UploadFileConfig(mime_type="video/mp4", display_name=nombre_ascii)
                )

            intentos = 0
            while gfile.state == types.FileState.PROCESSING and intentos < 30:
                time.sleep(2)
                gfile = client.files.get(name=gfile.name)
                intentos += 1

            if gfile.state != types.FileState.ACTIVE:
                raise RuntimeError(f"El archivo de video no pudo ser procesado por los servidores de Google (estado: {gfile.state}).")

            intentos_vision = 3
            for intento in range(1, intentos_vision + 1):
                try:
                    print(f"   [Visión IA - Gemini 3.6 Flash] Inspeccionando video con Gemini 3.6 Flash (Intento {intento}/{intentos_vision} | Clave #{num_clave})...")
                    res_vision = client.models.generate_content(
                        model="gemini-3.6-flash",
                        contents=[gfile, prompt_vision]
                    )
                    if res_vision and hasattr(res_vision, "text") and res_vision.text.strip():
                        cronologia = res_vision.text.strip()
                        gestor_pool.registrar_consumo_activo()
                        gestor_pool.registrar_exito_clave(key_activa)
                        print(f"   ✅ [Visión IA - Gemini 3.6 Flash] Bitácora visual extraída con éxito ({len(cronologia)} caracteres).")
                        break
                except Exception as ev:
                    ultimo_err_vision = str(ev)
                    err_v_str = ultimo_err_vision.lower()
                    print(f"   ⚠️ [Visión IA - Gemini 3.6 Flash] Intento {intento}/{intentos_vision} falló: {ultimo_err_vision[:120]}")
                    es_rot_vis = any(term in err_v_str for term in [
                        "429", "resource_exhausted", "quota", "401", "unauthenticated",
                        "invalid", "unsupported", "permission", "403"
                    ])
                    if es_rot_vis:
                        print(f"   🔄 [FALLO O CUOTA EN CLAVE #{num_clave}] Rotando automáticamente en visión...")
                        gestor_pool.rotar_siguiente_clave(f"Fallo en visión: {ultimo_err_vision[:80]}")
                        hubo_429 = True
                        break
                    if intento < intentos_vision:
                        time.sleep(4 * intento)

        except Exception as e_up:
            ultimo_err_vision = str(e_up)
            err_up_str = ultimo_err_vision.lower()
            print(f"   ⚠️ Error de subida para visión con Clave #{num_clave}: {ultimo_err_vision[:120]}")
            es_rot_up_vis = any(term in err_up_str for term in [
                "429", "resource_exhausted", "quota", "401", "unauthenticated",
                "invalid", "unsupported", "permission", "403"
            ])
            if es_rot_up_vis:
                print(f"   🔄 [FALLO O CUOTA EN CLAVE #{num_clave}] Rotando automáticamente en subida de visión...")
                gestor_pool.rotar_siguiente_clave(f"Fallo subida visión: {ultimo_err_vision[:80]}")
                hubo_429 = True
        finally:
            if gfile and client:
                try:
                    client.files.delete(name=gfile.name)
                except Exception:
                    pass

        if cronologia:
            return cronologia

        if not hubo_429 and not cronologia:
            # Si falló por una causa distinta a cuota (ej. error de video irrecuperable), no tiene sentido ciclar 10 veces
            break

    if not cronologia:
        raise RuntimeError(f"Gemini 3.6 Flash no pudo inspeccionar el video de la evidencia tras evaluar las claves del pool. Detalle: {ultimo_err_vision}")

    return cronologia


def analizar_evidencia_deepseek_groq(archivo_path: str, version_app: str = "4.4.1.08debug",
                                    dispositivo: str = "E800", descripcion: str = "",
                                    modelo: str = "openai/gpt-oss-120b",
                                    api_key: str = None,
                                    callback_paso=None) -> tuple:
    """
    Ejecuta el análisis técnico de QA utilizando Groq (OpenAI GPT-OSS 120B / Qwen 27B)
    en modalidad Híbrida (asistido exclusivamente por visión de Gemini 3.6 Flash para videos).
    Si Gemini 3.6 Flash no puede analizar el video, cancela el proceso con un error explicativo.
    Retorna (titulo_tarea, cuerpo_reporte, dict_datos).
    """
    if not api_key:
        cfg = cargar_configuracion_ia()
        api_key = cfg.get("groq_api_key", "").strip()

    if not api_key:
        raise ValueError("No se ha configurado la API Key de Groq. Ingrésala en la ventana o consíguela gratis en console.groq.com/keys.")

    # Resolver alias si se envió un nombre de modelo heredado o descontinuado
    modelo = MAPEO_MODELOS_ALIAS.get(modelo, modelo)
    if modelo not in MODELOS_GROQ_DISPONIBLES.values():
        modelo = "openai/gpt-oss-120b"

    ext = os.path.splitext(archivo_path)[1].lower()
    es_imagen = ext in [".png", ".jpg", ".jpeg"]
    tipo_texto = "captura de pantalla" if es_imagen else "grabación de video"
    nombre_archivo = os.path.basename(archivo_path)

    try:
        from subir_reporte_qa import obtener_fecha_espanol
        fecha_str = obtener_fecha_espanol()
    except Exception:
        import datetime
        meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        hoy = datetime.date.today()
        fecha_str = f"{hoy.day} de {meses[hoy.month - 1]} de {hoy.year}"

    # Si es video, extraer obligatoriamente la observación visual con Gemini 3.6 Flash
    cronologia_visual = ""
    if not es_imagen:
        if callback_paso:
            callback_paso("gemini_upload", "⏳", "Visión con Gemini 3.6...", "#c084fc")

        try:
            cronologia_visual = extraer_cronologia_visual_video(archivo_path)
        except Exception as e_vis:
            if callback_paso:
                callback_paso("gemini_upload", "❌", "Fallo visión Gemini 3.6", "#ef4444")
            raise RuntimeError(
                "El proceso no se pudo completar porque Gemini 3.6 Flash no pudo analizar el video de la evidencia.\n\n"
                f"Detalle técnico: {e_vis}\n\n"
                "Al usar Groq para la redacción, es estrictamente obligatorio que Gemini 3.6 Flash "
                "visualice e inspeccione el video previamente. La redacción ha sido cancelada para evitar "
                "generar un reporte incorrecto o sin análisis visual real."
            )

        if not cronologia_visual or not cronologia_visual.strip():
            if callback_paso:
                callback_paso("gemini_upload", "❌", "Fallo visión Gemini 3.6", "#ef4444")
            raise RuntimeError(
                "El proceso no se pudo completar porque Gemini 3.6 Flash no pudo analizar el video de la evidencia.\n\n"
                "Al usar Groq para la redacción, es estrictamente obligatorio que Gemini 3.6 Flash "
                "visualice e inspeccione el video previamente. La redacción ha sido cancelada para evitar "
                "generar un reporte incorrecto o sin análisis visual real."
            )

        if callback_paso:
            callback_paso("gemini_upload", "✅", "Listo (Gemini 3.6)", "#10b981")
            callback_paso("gemini_analysis", "⏳", "Razonando en Groq...", "#c084fc")

    # Prompt con directrices exactas del Gem de Humberto para On The Fly POS
    prompt_sistema = """Estamos enfocados en una aplicación para equipos Android en tablet encargados de vender productos a través de las aplicaciones llamadas On The Fly, las cuales se dividen en OTF Standard, OTF Air, OTF KDS (pantalla de cocina) y Express.

Registramos reportes de errores causados en el proceso de validación de pagos (pagos en efectivo, tarjetas débito, crédito), modos de pagos como (Split, Manual Entry, Payment Request, etc.), flujos de venta, pedidos, inventario, reembolsos y operativa del punto de venta.

Como QA altamente calificado necesitamos generar reportes en la plataforma Todoist con el formato oficial del equipo, con el máximo rigor técnico, quirúrgicos y siguiendo ESTRICTAMENTE la estructura requerida."""

    if cronologia_visual:
        contexto_evidencia = f"""INFORMACIÓN DEL ENTORNO Y OBSERVACIÓN VISUAL REAL EXTRAÍDA DEL VIDEO:
- Archivo de evidencia: {nombre_archivo} ({tipo_texto})
- Aplicación: On The Fly POS
- Versión bajo prueba: {version_app}
- Dispositivo: {dispositivo}
- Fecha de emisión: {fecha_str}

CRONOLOGÍA VISUAL EXACTA OBSERVADA EN EL TERMINAL POS (SEGUNDO A SEGUNDO):
\"\"\"{cronologia_visual}\"\"\"

Notas adicionales del evaluador:
{descripcion or 'Analiza la cronología visual anterior para deducir con exactitud el problema ocurrido, los pasos reproducibles y la causa raíz.'}
"""
    else:
        contexto_evidencia = f"""INFORMACIÓN DEL ENTORNO Y EVIDENCIA:
- Archivo de evidencia: {nombre_archivo} ({tipo_texto})
- Aplicación: On The Fly POS
- Versión bajo prueba: {version_app}
- Dispositivo: {dispositivo}
- Fecha de emisión: {fecha_str}
- Exposición textual del problema / comportamiento observado:
{descripcion or f'Se detectó una anomalía durante la prueba operativa en el archivo {nombre_archivo}.'}
"""

    instrucciones = f"""Debes generar el reporte técnico de QA respondiendo OBLIGATORIAMENTE con dos bloques claramente delimitados:

[TITULO]
[{version_app}] [Escribe aquí un título conciso, claro y profesional que resuma exactamente el fallo observado en ESPAÑOL]

[CUERPO]
**Fecha de emisión del reporte:** {fecha_str}
**Reportado por:** Humberto Rosales García

**Entorno:**
- **Dispositivo:** {dispositivo}
- **Versión de la App:** {version_app}

**Precondiciones:**
- [Condición necesaria 1 requerida antes de reproducir el error]
- [Condición necesaria 2]

**Definición del problema / Resultado Actual:**
[Descripción detallada del comportamiento erróneo observado y su impacto técnico en la aplicación]

**Procedimiento para llegar al error:**
1. [Paso numerado 1 de las acciones para reproducir el fallo]
2. [Paso numerado 2]
3. [Paso numerado 3]
(incluir todos los pasos necesarios identificados en orden cronológico)

**Resultado esperado:**
[Cómo debería comportarse el sistema correctamente según el estándar operativo]

**Código Afectado:** []

**Evidencias adjuntas:**

REGLAS ESTRICTAS E INQUEBRANTABLES:
1. IDIOMA 100% EN ESPAÑOL:
   - El TÍTULO DEBE SER REDACTADO OBLIGATORIAMENTE EN ESPAÑOL.
   - Aunque la pantalla o botones del POS tengan palabras en inglés (ej: 'Refund', 'Card Refund', 'Print Receipt', 'Sale Complete' o 'CARD EXPIRED'), el título debe redactarse en español.
   - PROHIBIDO TERMINANTEMENTE generar títulos en inglés como "Card Refund fails with...", "Receipt printing cut off...".
   - Ejemplo correcto de título: '[{version_app}] Error al procesar reembolso con tarjeta mostrando mensaje "CARD EXPIRED" en tarjeta activa'
   - Todas las secciones (Precondiciones, Definición del problema, Procedimiento, Resultado esperado) DEBEN redactarse en español técnico formal.

2. ESTRUCTURA COMPLETA (IDÉNTICA AL GEM DE QA):
   - NO resumas ni omitas secciones.
   - Es estrictamente obligatorio incluir la sección '**Procedimiento para llegar al error:**' detallando cronológicamente los pasos numerados del video.
   - Es estrictamente obligatorio incluir la sección '**Resultado esperado:**' indicando cómo debe comportarse el sistema correctamente. NUNCA finalices la respuesta sin incluir el resultado esperado.
"""

    messages = [
        {"role": "system", "content": prompt_sistema},
        {"role": "user", "content": contexto_evidencia + "\n" + instrucciones}
    ]

    payload_final = {
        "model": modelo,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4096
    }

    try:
        resp = ejecutar_llamada_openai_compatible(ENDPOINT_GROQ, api_key, payload_final, timeout=60.0)
    except Exception as e:
        # Fallback a openai/gpt-oss-120b o qwen si el modelo solicitado fallara
        if modelo != "openai/gpt-oss-120b":
            payload_final["model"] = "openai/gpt-oss-120b"
            resp = ejecutar_llamada_openai_compatible(ENDPOINT_GROQ, api_key, payload_final, timeout=60.0)
        else:
            raise e

    contenido = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

    # Limpiar posibles etiquetas de pensamiento <think>...</think> si vienen incluidas
    contenido_limpio = re.sub(r"<think>.*?</think>", "", contenido, flags=re.DOTALL).strip()

    # Extraer Título y Cuerpo
    match_titulo = re.search(r"\[TITULO\]\s*\n*(.*?)(?=\n*\[CUERPO\]|\Z)", contenido_limpio, flags=re.DOTALL | re.IGNORECASE)
    match_cuerpo = re.search(r"\[CUERPO\]\s*\n*(.*)", contenido_limpio, flags=re.DOTALL | re.IGNORECASE)

    if match_titulo and match_cuerpo:
        titulo_raw = match_titulo.group(1).strip()
        cuerpo = match_cuerpo.group(1).strip()
    else:
        lineas = contenido_limpio.split("\n", 1)
        titulo_raw = lineas[0].replace("#", "").strip()
        cuerpo = lineas[1].strip() if len(lineas) > 1 else contenido_limpio

    try:
        from subir_reporte_qa import asegurar_titulo_en_espanol, normalizar_cuerpo_gem
        titulo = asegurar_titulo_en_espanol(titulo_raw, version_app)
        cuerpo = normalizar_cuerpo_gem(cuerpo, fecha_str, version_app, dispositivo)
    except Exception:
        titulo = re.sub(r"[\r\n]+", " ", titulo_raw).strip()
        if not titulo.startswith("["):
            titulo = f"[{version_app}] {titulo}"

    # Limpiar posibles restos de divisores markdown al inicio del cuerpo
    lineas_cuerpo = cuerpo.splitlines()
    while lineas_cuerpo and re.match(r"^[*_#`\-\s=]+$", lineas_cuerpo[0].strip()):
        lineas_cuerpo.pop(0)
    cuerpo = "\n".join(lineas_cuerpo).strip()

    return titulo, cuerpo, {"modelo_usado": modelo}


def preanalizar_con_deepseek_groq(archivo_path: str, api_key: str = None) -> dict:
    """Pre-análisis rápido con Groq para autocompletar versión, dispositivo, prioridad y etiquetas."""
    if not api_key:
        cfg = cargar_configuracion_ia()
        api_key = cfg.get("groq_api_key", "").strip()

    if not api_key or not os.path.isfile(archivo_path):
        return {}

    ext = os.path.splitext(archivo_path)[1].lower()
    nombre_archivo = os.path.basename(archivo_path)
    es_video = ext in [".mp4", ".mov", ".m4v", ".avi"]

    # Para videos, Groq no tiene procesamiento de video nativo y NUNCA debe inventar un problema inexistente
    prompt_pre = f"""Actúa como un asistente técnico de QA de "On The Fly POS".
Analiza el nombre de archivo "{nombre_archivo}". Si contiene información de versión o dispositivo, extráela.
NO inventes fallos ni errores que no conozcas. Para videos, deja 'resumen_problema' vacío "".
Genera la información técnica en formato JSON estrictamente válido:
{{
  "version_app": "4.4.1.08debug",
  "dispositivo": "E800",
  "prioridad": 3,
  "etiquetas": ["Bug", "Apk Standard", "Offline"],
  "resumen_problema": ""
}}
Responde ÚNICAMENTE con el bloque JSON."""

    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "user", "content": prompt_pre}
        ],
        "temperature": 0.1,
        "max_tokens": 500
    }
    try:
        res = ejecutar_llamada_openai_compatible(ENDPOINT_GROQ, api_key, payload, timeout=20.0)
        txt = res.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        txt = re.sub(r"^```json\s*", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"^```\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
        data = json.loads(txt)
        if es_video:
            data["resumen_problema"] = ""
        return data
    except Exception:
        return {}
