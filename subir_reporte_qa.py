import os
import re
import sys
import time
import json
import argparse
import datetime
import subprocess
import requests
import unicodedata
import io
import msvcrt
import threading
import concurrent.futures
from google import genai
from google.genai import types, errors
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from gemini_cuotas import gestor_cuotas

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ==========================================
# 1. CONFIGURACIONES GENERALES
# ==========================================
from gemini_keys_pool import gestor_pool, enmascarar_clave
GEMINI_API_KEY = gestor_pool.obtener_clave_activa()[0]
TODOIST_API_TOKEN = "1d4235b0ddeafea41bceeb79d28ae65b340c8c46"

# Proyecto y sección en Todoist
PROJECT_NAME = "OTF Development"
SECTION_NAME = "Sprint Backlog"

# Carpeta raíz 'Soportes' en Google Drive (Compartidos conmigo)
SOPORTES_ROOT_FOLDER_ID = "1p_NYqN4JGXwJm4xDCvWgJ5tZEL1h7LdO"
DRIVE_FOLDER_ID = SOPORTES_ROOT_FOLDER_ID

# Carpeta local predeterminada donde se guardan las grabaciones
VIDEOS_DEFAULT_DIR = r"C:\Users\Administrador\Desktop\Reportes QA"

# Scopes de Google Drive
SCOPES = ['https://www.googleapis.com/auth/drive']

# Catálogo completo de etiquetas disponibles en Todoist
ETIQUETAS_DISPONIBLES = [
    "Bug",
    "Apk Standard",
    "Offline",
    "Online",
    "Apk Air",
    "Apk Air 2.0",
    "Apk Kiosk",
    "Apk Kds",
    "Backend",
    "Frontend",
    "En Español",
    "Old"
]

# Extensiones de evidencia admitidas
EXTENSIONES_VIDEO = ('.mp4',)
EXTENSIONES_IMAGEN = ('.png', '.jpg', '.jpeg')
EXTENSIONES_VALIDAS = EXTENSIONES_VIDEO + EXTENSIONES_IMAGEN


def obtener_mimetype(file_path: str) -> str:
    """Determina el MIME type según la extensión del archivo."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.mp4':
        return 'video/mp4'
    elif ext in ('.jpg', '.jpeg'):
        return 'image/jpeg'
    elif ext == '.png':
        return 'image/png'
    return 'application/octet-stream'


# ==========================================
# 2. FUNCIONES DE CANCELACIÓN Y REVERSIÓN (ESC)
# ==========================================
class ProcesoCanceladoException(Exception):
    """Excepción lanzada cuando el usuario cancela con la tecla ESC."""
    pass


def leer_input_con_escape(prompt: str = "") -> str:
    """Lee una línea de texto desde la consola permitiendo cancelar de inmediato con la tecla ESC o Ctrl+C."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buffer = []
    while True:
        try:
            ch = msvcrt.getwch()
        except Exception:
            return input()

        # Teclas especiales extendidas en Windows (flechas, etc.)
        if ch in ('\x00', '\xe0'):
            if msvcrt.kbhit():
                msvcrt.getwch()
            continue

        # Tecla ESC (código 27 / \x1b)
        if ch == '\x1b':
            sys.stdout.write('\n')
            sys.stdout.flush()
            raise ProcesoCanceladoException("Operación cancelada por el usuario con la tecla ESC.")

        # Ctrl + C
        elif ch == '\x03':
            sys.stdout.write('\n')
            sys.stdout.flush()
            raise KeyboardInterrupt()

        # Enter
        elif ch in ('\r', '\n'):
            sys.stdout.write('\n')
            sys.stdout.flush()
            return ''.join(buffer)

        # Backspace
        elif ch == '\x08':
            if buffer:
                buffer.pop()
                sys.stdout.write('\b \b')
                sys.stdout.flush()

        # Caracteres normales e imprimibles (incluyendo tildes, ñ, etc.)
        elif ord(ch) >= 32:
            buffer.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()


# Variable global y hook para actualizar interfaces visuales durante esperas con conteo regresivo
_cancelacion_solicitada = False
hook_conteo_espera = None
hook_cambio_paso = None


def notificar_paso(clave: str, estado: str, texto: str, color: str):
    """Notifica a la interfaz gráfica el cambio de estado de un paso del procedimiento."""
    global hook_cambio_paso
    if hook_cambio_paso:
        try:
            hook_cambio_paso(clave, estado, texto, color)
        except Exception:
            pass


def dormir_con_escape(segundos: float, callback_cada_segundo = None):
    """Pausa la ejecución permitiendo abortar en cualquier momento si se presiona ESC o Ctrl+C, reportando el conteo regresivo segundo a segundo."""
    global _cancelacion_solicitada, hook_conteo_espera
    inicio = time.time()
    ultimo_segundo_notificado = -1

    while (time.time() - inicio) < segundos:
        if _cancelacion_solicitada:
            _cancelacion_solicitada = False
            raise ProcesoCanceladoException("Operación cancelada o revertida por el usuario.")

        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch == '\x1b':
                raise ProcesoCanceladoException("Operación cancelada por el usuario con la tecla ESC.")
            elif ch == '\x03':
                raise KeyboardInterrupt()

        restante = max(0, int(round(segundos - (time.time() - inicio))))
        if restante != ultimo_segundo_notificado:
            ultimo_segundo_notificado = restante
            if callback_cada_segundo:
                try:
                    callback_cada_segundo(restante)
                except Exception:
                    pass
            if hook_conteo_espera:
                try:
                    hook_conteo_espera(restante)
                except Exception:
                    pass

        time.sleep(0.05)


def ejecutar_llamada_gemini_cancelable(client, kwargs_gen: dict, timeout_segundos: float = 35.0):
    """
    Ejecuta la llamada de generación de contenido en Gemini en un hilo daemon supervisado.
    Permite detectar cancelaciones del usuario de forma instantánea (en menos de 100ms)
    sin esperar respuestas bloqueantes de red ni congelar la interfaz gráfica.
    """
    global _cancelacion_solicitada
    resultado = [None]
    error = [None]
    terminado = threading.Event()

    def _worker():
        try:
            resultado[0] = client.models.generate_content(**kwargs_gen)
        except Exception as e:
            error[0] = e
        finally:
            terminado.set()

    hilo = threading.Thread(target=_worker, daemon=True)
    hilo.start()

    t_inicio = time.time()
    while not terminado.is_set():
        if _cancelacion_solicitada:
            _cancelacion_solicitada = False
            raise ProcesoCanceladoException("Operación cancelada por el usuario.")

        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch == '\x1b':
                raise ProcesoCanceladoException("Operación cancelada por el usuario con la tecla ESC.")
            elif ch == '\x03':
                raise KeyboardInterrupt()

        if (time.time() - t_inicio) > timeout_segundos:
            raise TimeoutError(f"La llamada al modelo excedió el límite de {timeout_segundos}s.")

        time.sleep(0.08)

    if error[0] is not None:
        raise error[0]

    return resultado[0]


def revertir_acciones(ruta_original: str = None, ruta_renombrada: str = None,
                      drive_service = None, drive_file_id: str = None,
                      task_id: str = None):
    """Revierte de inmediato cualquier cambio realizado si el usuario se arrepiente o cancela."""
    print("\n" + "=" * 60)
    print("🔄 REVIRTIENDO PROCESO COMPLETO POR CANCELACIÓN (ESC)...")
    print("=" * 60)

    # 1. Eliminar tarea de Todoist si ya se había creado
    if task_id:
        try:
            headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}"}
            res = requests.delete(f"https://api.todoist.com/api/v1/tasks/{task_id}", headers=headers)
            if res.status_code in (200, 204):
                print("   🗑️ Tarea eliminada de Todoist con éxito.")
            else:
                print(f"   ⚠️ No se pudo eliminar la tarea de Todoist (código {res.status_code}).")
        except Exception as e:
            print(f"   ⚠️ Error al intentar eliminar tarea de Todoist: {e}")

    # 2. Eliminar archivo de Google Drive si ya se había subido
    if drive_service and drive_file_id:
        try:
            drive_service.files().delete(fileId=drive_file_id, supportsAllDrives=True).execute()
            print("   🗑️ Archivo eliminado de Google Drive con éxito.")
        except Exception as e:
            print(f"   ⚠️ Error al intentar eliminar archivo de Drive: {e}")

    # 3. Restaurar nombre original del archivo local si fue renombrado
    if ruta_original and ruta_renombrada and ruta_original != ruta_renombrada:
        try:
            if os.path.exists(ruta_renombrada) and not os.path.exists(ruta_original):
                os.rename(ruta_renombrada, ruta_original)
                print(f"   📂 Nombre de archivo local restaurado al original:\n      {ruta_original}")
        except Exception as e:
            print(f"   ⚠️ Error al restaurar nombre local: {e}")

    print("=" * 60)
    print("🛑 ¡Proceso revertido! Ningún cambio quedó registrado.")
    print("=" * 60 + "\n")


# ==========================================
# 3. FUNCIONES DE AYUDA Y FORMATO
# ==========================================
def sanitizar_titulo(titulo: str, version_app: str = "4.4.1.08debug") -> str:
    """
    Limpia y estandariza de forma rigurosa el título del reporte de QA.
    Garantiza:
    1. Una sola línea (sin saltos de línea \\r o \\n).
    2. Sin artefactos de markdown (**, ##, ---, `, _, etc.).
    3. Sin etiquetas de prefijos duplicados (ej: [4.4.1.08debug] [4.4.1.08debug]).
    4. Sin etiquetas residuales de proyectos como [OTF Development].
    5. Formato final uniforme y estricto: "[{version_app}] {descripcion_limpia}".
    """
    if not titulo:
        return f"[{version_app}] Reporte de error detectado en prueba de QA"

    lineas = [l.strip() for l in str(titulo).splitlines() if l.strip()]
    texto_util = ""
    for l in lineas:
        # Descartar separadores markdown o bloques vacíos
        l_sin_md = re.sub(r"^[*_#`\-\s=]+|[*_#`\-\s=]+$", "", l).strip()
        if not l_sin_md:
            continue
        if re.fullmatch(r"t[ií]tulo\s*:?", l_sin_md, re.IGNORECASE):
            continue
        # Si la línea solo contiene la etiqueta de versión entre corchetes y nada más, ignorarla
        sin_corchetes = re.sub(r"\[\s*[^\]]+\s*\]", "", l_sin_md).strip()
        if not sin_corchetes:
            continue
        texto_util = l_sin_md
        break

    if not texto_util:
        texto_util = re.sub(r"[\r\n]+", " ", str(titulo)).strip()

    # Limpiar cualquier residuo de markdown
    texto_util = re.sub(r"[*_#`]", "", texto_util).strip()

    # Remover cualquier ocurrencia de [OTF Development] u OTF Development
    texto_util = re.sub(r"\[?\s*OTF\s+Development\s*\]?:?", "", texto_util, flags=re.IGNORECASE).strip()

    # Remover prefijos de versión repetidos existentes al inicio (ej: [4.4.1.08debug] [4.4.1.08debug])
    patron_version = re.compile(r"^\[\s*[^\]]+\s*\]\s*:?\s*", re.IGNORECASE)
    while patron_version.match(texto_util):
        texto_util = patron_version.sub("", texto_util).strip()

    # Eliminar posibles dos puntos, guiones o barras al inicio después de limpiar los corchetes
    texto_util = re.sub(r"^[:\-\s]+", "", texto_util).strip()

    if not texto_util:
        texto_util = "Reporte de error detectado en prueba de QA"

    # Dejar en una sola línea limpia (espacios sencillos)
    texto_util = re.sub(r"\s+", " ", texto_util).strip()

    return f"[{version_app}] {texto_util}"


def formatear_mensaje_whatsapp(titulo: str, task_url: str, drive_link: str,
                               es_imagen: bool = False, version_app: str = "4.4.1.08debug") -> str:
    """
    Construye el formato exacto y limpio para copiar y pegar en WhatsApp:
    
    [{version}] {descripcion_limpia}
    
    Link de Todoist: {task_url}
    
    Link del Video: {drive_link}
    """
    titulo_limpio = sanitizar_titulo(titulo, version_app)
    tipo_evidencia = "Link de la Captura" if es_imagen else "Link del Video"
    task_url_limpio = str(task_url).strip() if task_url else ""
    drive_link_limpio = str(drive_link).strip() if drive_link else ""

    partes = [
        titulo_limpio,
        "",
        f"Link de Todoist: {task_url_limpio}",
        "",
        f"{tipo_evidencia}: {drive_link_limpio}"
    ]
    return "\n".join(partes)


def limpiar_nombre_archivo(nombre: str) -> str:
    """Elimina caracteres no permitidos en nombres de archivo en Windows y asegura una sola línea."""
    nombre = re.sub(r"[\r\n]+", " ", str(nombre))
    limpio = re.sub(r'[\\/*?:"<>|]', "", nombre)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    return limpio


def obtener_fecha_espanol() -> str:
    """Retorna la fecha actual formateada en español (ej: 15 de septiembre de 2026)."""
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
    ]
    hoy = datetime.date.today()
    return f"{hoy.day} de {meses[hoy.month - 1]} de {hoy.year}"


def copiar_al_portapapeles(texto: str):
    """Copia texto al portapapeles de Windows de forma segura."""
    try:
        proceso = subprocess.Popen(['powershell', '-Command', 'Set-Clipboard -Value $input'],
                                   stdin=subprocess.PIPE, text=True)
        proceso.communicate(input=texto)
    except Exception:
        pass


def asegurar_titulo_en_espanol(titulo: str, version_app: str) -> str:
    """Verifica si el título fue generado en inglés y lo traduce automáticamente al español."""
    titulo_limpio = sanitizar_titulo(titulo, version_app)
    # Patrones comunes de títulos en inglés generados por LLMs
    patron_ingles = re.compile(
        r"\b(fails?|fails with|issue with|unable to|error despite|refund fails|crash when|printed|receipt with|cannot|doesn't|does not|error while|when trying|failing to)\b",
        re.IGNORECASE
    )
    if patron_ingles.search(titulo_limpio):
        try:
            from google import genai
            c, _, _ = gestor_pool.obtener_cliente()
            if c:
                prompt_tr = (
                    f"Traduce este título técnico de error de QA al español neutro profesional para Todoist. "
                    f"Mantén el tag [{version_app}] al inicio y traduce el resto al español. "
                    f"Responde ÚNICAMENTE con el título en español sin notas ni comillas:\n{titulo_limpio}"
                )
                r = c.models.generate_content(model="gemini-3.6-flash", contents=prompt_tr)
                if r and r.text and r.text.strip():
                    titulo_limpio = sanitizar_titulo(r.text.strip(), version_app)
        except Exception:
            pass
    return titulo_limpio


def normalizar_cuerpo_gem(cuerpo: str, fecha_str: str, version_app: str, dispositivo: str) -> str:
    """Normaliza el cuerpo del reporte para garantizar la maquetación exacta en negritas del Gem de Humberto."""
    # Asegurar Fecha de emisión y Reportado por con negritas
    if "**Fecha de emisión del reporte:**" not in cuerpo:
        cuerpo = re.sub(r"(?i)Fecha de emisi[oó]n del reporte:\s*", "**Fecha de emisión del reporte:** ", cuerpo)
    if "**Reportado por:**" not in cuerpo:
        cuerpo = re.sub(r"(?i)Reportado por:\s*", "**Reportado por:** ", cuerpo)

    # Si aún no tiene Reportado por, anteponerlo
    if "**Reportado por:** Humberto Rosales García" not in cuerpo and "Humberto Rosales García" not in cuerpo:
        cuerpo = f"**Fecha de emisión del reporte:** {fecha_str}\n**Reportado por:** Humberto Rosales García\n\n{cuerpo}"
    elif "Humberto Rosales García" not in cuerpo:
        cuerpo = re.sub(r"(?i)\*{0,2}Reportado por:\*{0,2}.*", "**Reportado por:** Humberto Rosales García", cuerpo)

    # Entorno
    cuerpo = re.sub(r"(?i)^\s*Entorno:\s*$", "**Entorno:**", cuerpo, flags=re.MULTILINE)
    cuerpo = re.sub(r"(?i)^-\s*Dispositivo:\s*", "- **Dispositivo:** ", cuerpo, flags=re.MULTILINE)
    cuerpo = re.sub(r"(?i)^-\s*Versi[oó]n de la App:\s*", "- **Versión de la App:** ", cuerpo, flags=re.MULTILINE)

    # Precondiciones
    cuerpo = re.sub(r"(?i)^\s*Precondiciones:\s*$", "**Precondiciones:**", cuerpo, flags=re.MULTILINE)

    # Definición del problema / Resultado Actual
    cuerpo = re.sub(r"(?i)^\s*Definici[oó]n del problema\s*/?\s*Resultado Actual:\s*$", "**Definición del problema / Resultado Actual:**", cuerpo, flags=re.MULTILINE)

    # Procedimiento para llegar al error
    cuerpo = re.sub(r"(?i)^\s*Procedimiento para llegar al error:\s*$", "**Procedimiento para llegar al error:**", cuerpo, flags=re.MULTILINE)

    # Resultado esperado
    cuerpo = re.sub(r"(?i)^\s*Resultado esperado:\s*$", "**Resultado esperado:**", cuerpo, flags=re.MULTILINE)

    # Código Afectado
    if "**Código Afectado:**" not in cuerpo and "Código Afectado:" not in cuerpo and "Codigo Afectado:" not in cuerpo:
        if "**Evidencias adjuntas:**" in cuerpo:
            cuerpo = cuerpo.replace("**Evidencias adjuntas:**", "**Código Afectado:** []\n\n**Evidencias adjuntas:**")
        elif "Evidencias adjuntas:" in cuerpo:
            cuerpo = cuerpo.replace("Evidencias adjuntas:", "**Código Afectado:** []\n\n**Evidencias adjuntas:**")
        else:
            cuerpo += "\n\n**Código Afectado:** []\n\n**Evidencias adjuntas:**"
    else:
        cuerpo = re.sub(r"(?i)\*{0,2}C[oó]digo Afectado:\*{0,2}\s*\[?\]?", "**Código Afectado:** []", cuerpo)

    # Evidencias adjuntas
    cuerpo = re.sub(r"(?i)^\s*Evidencias adjuntas:\s*$", "**Evidencias adjuntas:**", cuerpo, flags=re.MULTILINE)

    return cuerpo


def construir_prompt_qa(version: str, dispositivo: str, fecha_str: str,
                        es_imagen: bool = False, descripcion_problema: str = "") -> str:
    """Construye el prompt especializado para el Agente de QA en Gemini siguiendo fielmente las directrices del Gem de Humberto."""
    if es_imagen:
        instruccion_base = (
            "Analiza detalladamente la captura de pantalla (imagen) adjunta y evalúa el bug reportado, "
            "tomando como referencia tanto la evidencia visual como la descripción textual provista por el tester."
        )
    else:
        instruccion_base = (
            "Analiza detalladamente la grabación de pantalla adjunta donde se evidencia un bug en la aplicación."
        )

    contexto_problema = ""
    if descripcion_problema and descripcion_problema.strip():
        contexto_problema = f"""
EXPOSICIÓN / CONTEXTO DEL PROBLEMA OBSERVADO POR EL TESTER:
\"\"\"
{descripcion_problema.strip()}
\"\"\"
"""

    return f"""Estamos enfocados en una aplicación para equipos Android en tablet encargados de vender productos a través de las aplicaciones llamadas On The Fly, las cuales se dividen en OTF Standard, OTF Air, OTF KDS (pantalla de cocina) y Express.

Registramos reportes de errores causados en el proceso de validación de pagos (pagos en efectivo, tarjetas débito, crédito), modos de pagos como (Split, Manual Entry, Payment Request, etc.), flujos de venta, pedidos, inventario, reembolsos y operativa del punto de venta.

Como QA altamente calificado necesitamos generar reportes en la plataforma Todoist con el formato oficial del equipo.
{instruccion_base}
{contexto_problema}
Genera el reporte técnico del error detectado siguiendo ESTRICTAMENTE esta estructura y reglas:

TITULO:
[{version}] [Descripción clara y concisa del error en ESPAÑOL]

**Fecha de emisión del reporte:** {fecha_str}
**Reportado por:** Humberto Rosales García

**Entorno:**
- **Dispositivo:** {dispositivo}
- **Versión de la App:** {version}

**Precondiciones:**
- [Condición necesaria 1 requerida antes de reproducir el error]
- [Condición necesaria 2]

**Definición del problema / Resultado Actual:**
[Descripción detallada del comportamiento erróneo observado y su impacto técnico en la aplicación]

**Procedimiento para llegar al error:**
1. [Paso numerado 1 detallado de las acciones para reproducir el fallo]
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
   - Ejemplo correcto de título: '[{version}] Error al procesar reembolso con tarjeta mostrando mensaje "CARD EXPIRED" en tarjeta activa'
   - Todas las secciones (Precondiciones, Definición del problema, Procedimiento, Resultado esperado) DEBEN redactarse en español técnico formal.

2. ESTRUCTURA COMPLETA (IDÉNTICA AL GEM DE QA):
   - NO resumas ni omitas secciones.
   - Es estrictamente obligatorio incluir la sección '**Procedimiento para llegar al error:**' detallando cronológicamente los pasos numerados del video.
   - Es estrictamente obligatorio incluir la sección '**Resultado esperado:**' indicando cómo debe comportarse el sistema correctamente. NUNCA finalices la respuesta sin incluir el resultado esperado.
"""


def extraer_titulo_y_cuerpo(reporte_crudo: str, version_app: str) -> tuple:
    """Extrae de forma robusta el título y el cuerpo del reporte asegurando formato impecable idéntico al Gem."""
    lineas = reporte_crudo.strip().splitlines()
    titulo_candidato = ""
    indice_cuerpo = 0

    for i, linea in enumerate(lineas):
        if re.search(r"T[IÍ]TULO\s*:?", linea, re.IGNORECASE):
            contenido_mismalinea = re.sub(r"^.*?T[IÍ]TULO\s*:?\s*", "", linea, flags=re.IGNORECASE).strip()
            contenido_mismalinea = re.sub(r"^[*_#`\s]+|[*_#`\s]+$", "", contenido_mismalinea).strip()
            sin_tag = re.sub(r"\[\s*[^\]]+\s*\]", "", contenido_mismalinea).strip()
            if sin_tag:
                titulo_candidato = contenido_mismalinea
                indice_cuerpo = i + 1
                break
            elif i + 1 < len(lineas):
                for j in range(i + 1, min(i + 5, len(lineas))):
                    cand = re.sub(r"^[*_#`\s]+|[*_#`\s]+$", "", lineas[j]).strip()
                    if cand and not cand.startswith("---") and not cand.startswith("##"):
                        if re.sub(r"\[\s*[^\]]+\s*\]", "", cand).strip():
                            titulo_candidato = cand
                            indice_cuerpo = j + 1
                            break
                if titulo_candidato:
                    break

    if not titulo_candidato:
        for i, linea in enumerate(lineas):
            linea_limpia = re.sub(r"^[*_#`\s]+|[*_#`\s]+$", "", linea).strip()
            if linea_limpia and not re.search(r"^(?:Fecha|Reportado|Entorno|Precondiciones|\*\*|---|###|===)", linea_limpia, re.IGNORECASE):
                if re.sub(r"\[\s*[^\]]+\s*\]", "", linea_limpia).strip():
                    titulo_candidato = linea_limpia
                    indice_cuerpo = i + 1
                    break

    titulo_tarea = asegurar_titulo_en_espanol(titulo_candidato, version_app)

    # Limpiar posibles restos de líneas divisorias o markdown al inicio del cuerpo
    lineas_cuerpo = lineas[indice_cuerpo:]
    while lineas_cuerpo and re.match(r"^[*_#`\-\s=]+$", lineas_cuerpo[0].strip()):
        lineas_cuerpo.pop(0)

    cuerpo = "\n".join(lineas_cuerpo).strip()
    if not cuerpo:
        cuerpo = reporte_crudo

    cuerpo = normalizar_cuerpo_gem(cuerpo, obtener_fecha_espanol(), version_app, "E800")

    return titulo_tarea, cuerpo


# ==========================================
# 3. AUTENTICACIÓN Y SUBIDA A GOOGLE DRIVE
# ==========================================
def autenticar_drive():
    """Autentica con la API de Google Drive usando OAuth 2.0 y guarda token.json."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    token_path = os.path.join(script_dir, 'token.json')
    credentials_path = os.path.join(script_dir, 'credentials.json')

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"\n⚠️ El token de Google Drive expiró o fue revocado ({e}).")
                print("🔑 Se abrirá el navegador para renovar la autorización de Google Drive...")
                creds = None

        if not creds or not creds.valid:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(f"No se encontró el archivo de credenciales: {credentials_path}")
            print("\n🔑 Abriendo navegador para autorizar acceso a Google Drive...")
            if hook_cambio_paso:
                try:
                    hook_cambio_paso("drive_upload", "🔑", "Autorizando en navegador...", "#f59e0b")
                except Exception:
                    pass
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, 'w', encoding='utf-8') as token_file:
            token_file.write(creds.to_json())

    return build('drive', 'v3', credentials=creds)


def subir_a_drive(service, file_path: str, nuevo_nombre: str, folder_id: str) -> tuple:
    """Sube el archivo (video o imagen) a Google Drive y retorna (drive_link, file_id)."""
    ext = os.path.splitext(file_path)[1].lower()
    if not nuevo_nombre.lower().endswith(ext):
        nombre_final = f"{nuevo_nombre}{ext}"
    else:
        nombre_final = nuevo_nombre

    file_metadata = {
        'name': nombre_final,
        'parents': [folder_id]
    }
    mimetype = obtener_mimetype(file_path)
    media = MediaFileUpload(file_path, mimetype=mimetype, resumable=True)

    print(f"   ⏳ Subiendo '{nombre_final}' a Google Drive...")
    archivo = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id, webViewLink',
        supportsAllDrives=True
    ).execute()
    file_id = archivo.get('id')

    # Compartir públicamente para que el equipo pueda abrirlo
    try:
        service.permissions().create(
            fileId=file_id,
            body={'type': 'anyone', 'role': 'reader'},
            supportsAllDrives=True
        ).execute()
    except Exception as e:
        print(f"   ⚠️ Aviso sobre permisos públicos de Drive: {e}")

    drive_link = f"https://drive.google.com/file/d/{file_id}/view?usp=drive_link"
    return drive_link, file_id


def obtener_o_crear_carpeta(service, nombre: str, parent_id: str) -> str:
    """Busca una carpeta por nombre exacto dentro del parent_id; si no existe, la crea y le asigna permisos públicos."""
    q = f"'{parent_id}' in parents and name = '{nombre}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(
        q=q,
        fields='files(id, name)',
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    archivos = res.get('files', [])
    if archivos:
        return archivos[0]['id']

    # Crear carpeta si no existe
    metadata = {
        'name': nombre,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    nueva = service.files().create(
        body=metadata,
        fields='id, name',
        supportsAllDrives=True
    ).execute()
    nueva_id = nueva.get('id')

    try:
        service.permissions().create(
            fileId=nueva_id,
            body={'type': 'anyone', 'role': 'reader'},
            supportsAllDrives=True
        ).execute()
    except Exception:
        pass

    return nueva_id


def buscar_carpeta_aproximada(service, candidatos: list, parent_id: str) -> str:
    """Busca si existe alguna carpeta que coincida con alguno de los nombres candidatos (case-insensitive)."""
    q = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = service.files().list(
        q=q,
        fields='files(id, name)',
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    existentes = {f['name'].lower(): f['id'] for f in res.get('files', [])}
    for cand in candidatos:
        if cand.lower() in existentes:
            return existentes[cand.lower()]
    return None


def determinar_producto(etiquetas: list = None, descripcion: str = "", titulo: str = "", producto_forzado: str = "Auto") -> str:
    """
    Determina de forma inteligente la carpeta principal de producto en Drive:
    Standard, Air, Backend, KDS, Kiosk o Express.
    """
    if producto_forzado and producto_forzado not in ["Auto", "Auto-detectar", "Auto-detectar (Según etiquetas y descripción)"]:
        return producto_forzado

    etiquetas = etiquetas or []
    texto_combinado = f"{' '.join(etiquetas)} {descripcion} {titulo}".lower()

    if any(k in texto_combinado for k in ["backend", "backoffice", "servidor", "api", "base de datos", "database"]):
        return "Backend"
    elif any(k in texto_combinado for k in ["apk air", "apk air 2.0", "air"]):
        return "Air"
    elif any(k in texto_combinado for k in ["apk kiosk", "kiosk", "kiosko", "quiosco"]):
        return "Kiosk"
    elif any(k in texto_combinado for k in ["apk kds", "kds", "pantalla de cocina"]):
        return "KDS"
    elif any(k in texto_combinado for k in ["express"]):
        return "Express"
    else:
        return "Standard"


def resolver_carpeta_destino(service, producto: str = "Standard", version_app: str = "4.4.1.08debug") -> tuple:
    """
    Navega la estructura de Google Drive dentro de 'Soportes' y crea las carpetas necesarias
    según el producto y la versión de la aplicación.
    Retorna (folder_id, ruta_legible).
    """
    # 1. Obtener o crear la carpeta del producto (Standard, Air, Backend, etc.)
    producto_id = obtener_o_crear_carpeta(service, producto, SOPORTES_ROOT_FOLDER_ID)
    ruta_legible = f"Soportes / {producto}"

    if not version_app or producto == "Backend":
        return producto_id, ruta_legible

    # 2. Descomponer versión para navegación jerárquica (ej: 4.4.1.08debug -> 4.4 / .1 / .08)
    v_clean = version_app.strip()
    match = re.match(r"^(\d+\.\d+)\.(\d+)(?:\.([0-9a-zA-Z]+))?", v_clean)
    if match:
        nivel1 = match.group(1)        # ej: 4.4
        nivel2 = f".{match.group(2)}"  # ej: .1
        build_raw = match.group(3)     # ej: 08debug o 08

        # Nivel 1 (ej: 4.4)
        n1_id = obtener_o_crear_carpeta(service, nivel1, producto_id)
        ruta_legible += f" / {nivel1}"

        # Nivel 2 (ej: .1)
        n2_id = obtener_o_crear_carpeta(service, nivel2, n1_id)
        ruta_legible += f" / {nivel2}"

        if build_raw:
            build_num = re.sub(r"[^\d]", "", build_raw)
            candidatos = [f".{build_raw}", f".{build_num}", build_raw, build_num]
            candidatos = [c for c in candidatos if c]

            n3_id = buscar_carpeta_aproximada(service, candidatos, n2_id)
            if not n3_id:
                nombre_n3 = f".{build_num.zfill(2)}" if build_num else f".{build_raw}"
                n3_id = obtener_o_crear_carpeta(service, nombre_n3, n2_id)
                ruta_legible += f" / {nombre_n3}"
            else:
                try:
                    f_n3 = service.files().get(fileId=n3_id, fields='name', supportsAllDrives=True).execute()
                    ruta_legible += f" / {f_n3['name']}"
                except Exception:
                    ruta_legible += f" / .{build_raw}"
            return n3_id, ruta_legible
        else:
            return n2_id, ruta_legible
    else:
        # Versión no estándar (ej: '4.4', 'v2.1', etc.)
        sub_id = obtener_o_crear_carpeta(service, v_clean, producto_id)
        ruta_legible += f" / {v_clean}"
        return sub_id, ruta_legible


# ==========================================
# 4. GESTIÓN CON TODOIST (API v1)
# ==========================================
def obtener_ids_todoist():
    """Obtiene el ID del proyecto y de la sección en Todoist mediante API v1."""
    headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}"}

    res_p = requests.get("https://api.todoist.com/api/v1/projects", headers=headers)
    if res_p.status_code != 200:
        raise RuntimeError(f"Error consultando proyectos en Todoist: {res_p.status_code} - {res_p.text}")

    proyectos = res_p.json().get('results', [])
    project_id = next((p['id'] for p in proyectos if p.get('name') == PROJECT_NAME), None)

    if not project_id:
        raise ValueError(f"No se encontró el proyecto '{PROJECT_NAME}' en tu cuenta de Todoist.")

    res_s = requests.get(f"https://api.todoist.com/api/v1/sections?project_id={project_id}", headers=headers)
    if res_s.status_code != 200:
        raise RuntimeError(f"Error consultando secciones en Todoist: {res_s.status_code} - {res_s.text}")

    secciones = res_s.json().get('results', [])
    section_id = next((s['id'] for s in secciones if s.get('name') == SECTION_NAME), None)

    if not section_id:
        raise ValueError(f"No se encontró la sección '{SECTION_NAME}' dentro del proyecto '{PROJECT_NAME}'.")

    return project_id, section_id


def crear_tarea_todoist(titulo: str, descripcion: str, project_id: str, section_id: str,
                        prioridad: int = 2, labels: list = None) -> dict:
    """Crea la tarea de QA en Todoist con la prioridad y etiquetas correspondientes."""
    if labels is None:
        labels = ["Bug", "Apk Standard", "Offline"]

    headers = {
        "Authorization": f"Bearer {TODOIST_API_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "content": titulo,
        "description": descripcion,
        "project_id": project_id,
        "section_id": section_id,
        "priority": prioridad,
        "labels": labels
    }
    res = requests.post("https://api.todoist.com/api/v1/tasks", headers=headers, json=payload)
    if res.status_code not in (200, 204):
        # Si el proyecto 'OTF Development' alcanzó el límite de 300 tareas activas de Todoist
        error_text = res.text
        if "MAX_ITEMS_LIMIT_REACHED" in error_text or "Maximum number of items" in error_text:
            print("\n⚠️ El proyecto 'OTF Development' alcanzó el límite de 300 tareas activas permitido por Todoist.")
            print("⚡ Creando la tarea automáticamente en la Bandeja de Entrada (Inbox) para no perder tu reporte...")
            payload_inbox = {
                "content": titulo,
                "description": descripcion,
                "priority": prioridad,
                "labels": labels
            }
            res_inbox = requests.post("https://api.todoist.com/api/v1/tasks", headers=headers, json=payload_inbox)
            if res_inbox.status_code in (200, 204):
                tarea_inbox = res_inbox.json()
                tarea_inbox["_inbox_fallback"] = True
                print("✅ Tarea creada con éxito en tu Inbox de Todoist.")
                return tarea_inbox

        raise RuntimeError(f"Error al crear tarea en Todoist: {res.status_code} - {res.text}")

    return res.json()


# ==========================================
# 5. FLUJO PRINCIPAL Y PRE-ANÁLISIS IA
# ==========================================
def analizar_evidencia_previa(archivo_path: str, modelo_gemini: str = "gemini-3.6-flash",
                              proveedor_ia: str = "gemini", modelo_alternativo: str = "openai/gpt-oss-120b",
                              api_key_alternativa: str = "") -> dict:
    """
    Analiza preliminarmente la evidencia (video o imagen) con Gemini o DeepSeek/Groq para detectar:
    - version_app (ej: '4.4.1.08debug')
    - dispositivo ('E800', 'PAX A920Pro', etc.)
    - prioridad (1: P1, 2: P2, 3: P3, 4: P4) según la gravedad operativa del POS
    - etiquetas (lista de tags sugeridos)
    - resumen_problema (1-2 oraciones)
    Retorna un diccionario con estos datos o un dict vacío {} si no fue posible.
    """
    if proveedor_ia == "deepseek_groq":
        try:
            from proveedor_alternativo import preanalizar_con_deepseek_groq
            return preanalizar_con_deepseek_groq(archivo_path, api_key=api_key_alternativa)
        except Exception:
            return {}

    client, key_act, _ = gestor_pool.obtener_cliente()
    if not key_act or not os.path.isfile(archivo_path):
        return {}

    gemini_file = None
    try:
        mimetype = obtener_mimetype(archivo_path)
        nombre_base = os.path.basename(archivo_path)
        nombre_ascii = unicodedata.normalize('NFKD', nombre_base).encode('ascii', 'ignore').decode('ascii')
        if not nombre_ascii.strip():
            nombre_ascii = "evidencia_preanalisis"

        with open(archivo_path, "rb") as f_evidencia:
            gemini_file = client.files.upload(
                file=f_evidencia,
                config=types.UploadFileConfig(
                    mime_type=mimetype,
                    display_name=nombre_ascii[:40]
                )
            )

        # Si es video, esperar a que esté ACTIVE
        intentos = 0
        while gemini_file.state == types.FileState.PROCESSING and intentos < 25:
            time.sleep(2)
            gemini_file = client.files.get(name=gemini_file.name)
            intentos += 1

        if gemini_file.state != types.FileState.ACTIVE:
            return {}

        prompt_pre = """Actúa como un experto QA de la aplicación "On The Fly POS" (punto de venta para restaurantes y retail).
Analiza detenidamente la pantalla de este video o captura de pantalla.
Extrae la información técnica en formato JSON estrictamente válido con las siguientes claves:
{
  "version_app": "La versión de la app visible en la pantalla (ejemplo: 4.4.1.08debug, 4.4.1.09debug, etc.). Si no es visible, pon '4.4.1.08debug'",
  "dispositivo": "E800" si parece una pantalla horizontal de tablet/caja grande, o "PAX A920Pro" si es pantalla vertical de terminal táctil portátil / datáfono, o "PAX A910S", "Sunmi T2". Si no estás seguro, pon "E800",
  "prioridad": número entero del 1 al 4 según el impacto en la operación del POS:
      1: P1 Urgente (Bloqueo total de ventas, cobros con tarjeta caídos, crash que cierra la app, loop infinito)
      2: P2 Alta (Error en cálculos de impuestos/propinas, descuentos incorrectos, fallos en envío a cocina, fallo que afecta el flujo comercial)
      3: P3 Media (Glitch funcional recuperable, mensaje de error secundario, botón desalineado pero operable)
      4: P4 Baja (Detalle cosmético menor, traducción, ortografía, espaciado visual),
  "etiquetas": lista con 1 a 3 etiquetas sugeridas entre: ["Bug", "Apk Standard", "Offline", "Frontend", "Backend", "Crash", "UI/UX"],
  "resumen_problema": "Explicación concisa en 1 o 2 oraciones de lo que falla o se evidencia en el video/imagen"
}
Responde ÚNICAMENTE con el bloque JSON."""

        modelos_pre = [modelo_gemini]
        if modelo_gemini in ["gemini-3.8-flash", "gemini-3.6-flash"]:
            alt_pre = "gemini-3.6-flash" if modelo_gemini == "gemini-3.8-flash" else "gemini-3.8-flash"
            modelos_pre.append(alt_pre)

        resp = None
        for mod_pre in modelos_pre:
            try:
                resp = ejecutar_llamada_gemini_cancelable(
                    client,
                    {"model": mod_pre, "contents": [gemini_file, prompt_pre]},
                    timeout_segundos=25.0
                )
                if resp and resp.text:
                    gestor_cuotas.registrar_exito(mod_pre)
                    break
            except Exception as e:
                err_str = str(e).lower()
                if "429" in err_str or "resource_exhausted" in err_str:
                    gestor_cuotas.registrar_429(mod_pre, str(e))
                continue

        if resp and resp.text:
            txt = resp.text.strip()
            txt = re.sub(r"^```json\s*", "", txt, flags=re.IGNORECASE)
            txt = re.sub(r"^```\s*", "", txt)
            txt = re.sub(r"\s*```$", "", txt)
            return json.loads(txt)
    except Exception as e:
        return {}
    finally:
        if client and gemini_file:
            try:
                client.files.delete(name=gemini_file.name)
            except Exception:
                pass
    return {}


def procesar_evidencia(archivo_path: str, version_app: str = "4.4.1.08debug", dispositivo: str = "E800",
                       prioridad_ui: int = 3, etiquetas: list = None, renombrar_local: bool = True,
                       descripcion_problema: str = "", modelo_gemini: str = "gemini-3.6-flash",
                       razonamiento_ampliado: bool = False, modulo_producto: str = "Auto",
                       proveedor_ia: str = "gemini", modelo_alternativo: str = "deepseek-r1-distill-llama-70b",
                       api_key_alternativa: str = ""):
    """Ejecuta el ciclo de vida completo de análisis, renombrado, subida y reporte para video o imagen."""
    global _cancelacion_solicitada
    if not os.path.isfile(archivo_path):
        raise FileNotFoundError(f"El archivo especificado no existe: {archivo_path}")

    ext = os.path.splitext(archivo_path)[1].lower()
    if ext not in EXTENSIONES_VALIDAS:
        raise ValueError(f"Extensión no soportada: {ext}. Formatos permitidos: {', '.join(EXTENSIONES_VALIDAS)}")

    es_imagen = ext in EXTENSIONES_IMAGEN
    tipo_texto = "imagen" if es_imagen else "video"
    icono_tipo = "🖼️ Imagen:" if es_imagen else "📹 Video:"

    if not etiquetas:
        etiquetas = ["Bug", "Apk Standard", "Offline"]

    motor_str = f"{modelo_alternativo} (DeepSeek / Groq)" if proveedor_ia == "deepseek_groq" else f"{modelo_gemini} (Gemini)"
    print("\n" + "=" * 60)
    print("🚀 INICIANDO PROCESO AUTOMATIZADO DE REPORTE QA")
    print("=" * 60)
    print(f"{icono_tipo}        {archivo_path}")
    if descripcion_problema:
        print(f"📝 Problema:     {descripcion_problema}")
    print(f"🧠 Motor IA:     {motor_str}")
    print(f"📦 Versión App:  {version_app}")
    print(f"📱 Dispositivo:  {dispositivo}")
    print(f"🏷️  Etiquetas:    {', '.join(etiquetas)}")
    print("=" * 60)

    # Variables para rastrear y revertir en caso de arrepentimiento o cancelación (ESC)
    archivo_actual_path = archivo_path
    service_drive = None
    drive_file_id = None
    task_id = None

    try:
        titulo_tarea = ""
        cuerpo_reporte = ""

        if proveedor_ia == "deepseek_groq":
            notificar_paso("gemini_upload", "⏳", "Visión de evidencia...", "#c084fc")
            print(f"\n[1/4] 🟣 Extrayendo cronología visual de {tipo_texto} para motor de razonamiento...")

            from proveedor_alternativo import analizar_evidencia_deepseek_groq
            titulo_tarea, cuerpo_reporte, info_extra = analizar_evidencia_deepseek_groq(
                archivo_path=archivo_path,
                version_app=version_app,
                dispositivo=dispositivo,
                descripcion=descripcion_problema,
                modelo=modelo_alternativo,
                api_key=api_key_alternativa,
                callback_paso=notificar_paso
            )
            notificar_paso("gemini_upload", "✅", "Listo", "#10b981")
            notificar_paso("gemini_analysis", "✅", "Listo", "#10b981")
        else:
            # Flujo con Pool de 10 API Keys de Google Gemini y conmutación automática ante 429
            fecha_hoy = obtener_fecha_espanol()
            prompt = construir_prompt_qa(
                version=version_app,
                dispositivo=dispositivo,
                fecha_str=fecha_hoy,
                es_imagen=es_imagen,
                descripcion_problema=descripcion_problema
            )

            config_gen = None
            if razonamiento_ampliado:
                try:
                    config_gen = types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_budget=2048),
                        max_output_tokens=8192,
                        temperature=0.2
                    )
                except Exception:
                    config_gen = types.GenerateContentConfig(max_output_tokens=8192, temperature=0.2)
            else:
                config_gen = types.GenerateContentConfig(max_output_tokens=8192, temperature=0.2)

            # Prioridad estricta para Gemini 3.6 Flash y Gemini 3.8 Flash
            if modelo_gemini == "gemini-3.8-flash":
                modelos_candidatos = ["gemini-3.8-flash", "gemini-3.6-flash"]
            else:
                modelos_candidatos = ["gemini-3.6-flash", "gemini-3.8-flash"]

            response = None
            ultimo_error = ""
            total_claves_pool = len(gestor_pool.datos.get("claves", [])) or 10
            client = None
            gemini_file = None

            for intento_rotacion in range(total_claves_pool):
                if response is not None:
                    break

                if _cancelacion_solicitada:
                    _cancelacion_solicitada = False
                    raise ProcesoCanceladoException("Operación cancelada por el usuario.")

                client, clave_activa, num_clave = gestor_pool.obtener_cliente()
                mimetype = obtener_mimetype(archivo_path)
                nombre_base = os.path.basename(archivo_path)
                nombre_ascii = unicodedata.normalize('NFKD', nombre_base).encode('ascii', 'ignore').decode('ascii')
                if not nombre_ascii.strip():
                    nombre_ascii = f"evidencia{ext}"

                # 1. Subir archivo a Gemini con la API Key activa
                notificar_paso("gemini_upload", "⏳", f"Subiendo (Clave #{num_clave})...", "#3b82f6")
                print(f"\n[1/4] 🤖 Subiendo {tipo_texto} a Gemini (API Key #{num_clave}/{total_claves_pool}: {enmascarar_clave(clave_activa)})...")

                subida_exitosa = False
                hubo_429 = False

                try:
                    with open(archivo_path, "rb") as f_evidencia:
                        gemini_file = client.files.upload(
                            file=f_evidencia,
                            config=types.UploadFileConfig(
                                mime_type=mimetype,
                                display_name=nombre_ascii
                            )
                        )
                    print(f"   Archivo recibido por Gemini (ID: {gemini_file.name}). Esperando procesamiento...")

                    while gemini_file.state == types.FileState.PROCESSING:
                        print(f"   ⏳ Procesando {tipo_texto} en Gemini... [ESC para cancelar]", flush=True)
                        dormir_con_escape(2)
                        gemini_file = client.files.get(name=gemini_file.name)

                    if gemini_file.state != types.FileState.ACTIVE:
                        raise RuntimeError(f"El procesamiento del archivo en Gemini falló con estado: {gemini_file.state}")

                    subida_exitosa = True
                    notificar_paso("gemini_upload", "✅", "Listo", "#10b981")
                except (ProcesoCanceladoException, KeyboardInterrupt):
                    raise
                except Exception as e_up:
                    ultimo_error = str(e_up)
                    err_up_str = ultimo_error.lower()
                    print(f"   ⚠️ Falló la subida con API Key #{num_clave}: {ultimo_error[:140]}")
                    es_rotacion_up = any(term in err_up_str for term in [
                        "429", "resource_exhausted", "quota", "401", "unauthenticated",
                        "invalid", "unsupported", "permission", "403"
                    ])
                    if es_rotacion_up:
                        print(f"   🔄 [FALLO O CUOTA EN CLAVE #{num_clave}] Conmutando automáticamente a la siguiente API Key...")
                        notificar_paso("gemini_upload", "🔄", f"Rotando Clave #{num_clave}...", "#f59e0b")
                        gestor_pool.rotar_siguiente_clave(motivo=f"Fallo en subida: {ultimo_error[:80]}")
                        dormir_con_escape(1)
                        continue
                    else:
                        raise

                if not subida_exitosa or gemini_file is None:
                    continue

                # 2. Generar reporte con Gemini
                notificar_paso("gemini_analysis", "⏳", f"Analizando (Clave #{num_clave})...", "#3b82f6")
                print(f"\n[2/4] 🧠 Analizando {tipo_texto} y redactando reporte de QA con {modelo_gemini}...")

                for idx_mod, modelo_actual in enumerate(modelos_candidatos):
                    if response is not None or hubo_429:
                        break

                    if _cancelacion_solicitada:
                        _cancelacion_solicitada = False
                        raise ProcesoCanceladoException("Operación cancelada por el usuario.")

                    if idx_mod > 0:
                        print(f"\n⚡ Conmutando a '{modelo_actual}' para mantener la máxima calidad técnica sin degradar el análisis...")
                        notificar_paso("gemini_analysis", "⏳", f"Probando {modelo_actual}...", "#3b82f6")

                    # Dar hasta 4 intentos a 3.6 Flash y 2 a 3.8 Flash para superar picos de demanda
                    intentos_modelo = 4 if modelo_actual == "gemini-3.6-flash" else 2
                    for intento in range(1, intentos_modelo + 1):
                        if _cancelacion_solicitada:
                            _cancelacion_solicitada = False
                            raise ProcesoCanceladoException("Operación cancelada por el usuario.")

                        try:
                            kwargs_gen = {"model": modelo_actual, "contents": [gemini_file, prompt]}
                            if config_gen:
                                kwargs_gen["config"] = config_gen

                            print(f"   Consultando a {modelo_actual} (Intento {intento}/{intentos_modelo} | Clave #{num_clave})...")

                            response = ejecutar_llamada_gemini_cancelable(client, kwargs_gen, timeout_segundos=150.0)

                            if response and hasattr(response, "text") and response.text.strip():
                                gestor_pool.registrar_consumo_activo()
                                gestor_pool.registrar_exito_clave(clave_activa)
                                gestor_cuotas.registrar_consumo(modelo_actual, 1)
                                gestor_cuotas.registrar_exito(modelo_actual)

                            if hasattr(response, "usage_metadata") and response.usage_metadata:
                                p_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                                c_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
                                tot_tok = getattr(response.usage_metadata, "total_token_count", 0) or (p_tok + c_tok)
                                print(f"\n   [TOKENS GEMINI] Modelo: {modelo_actual} | Clave #{num_clave} | Entrada: {p_tok:,} | Salida: {c_tok:,} | Total: {tot_tok:,} tokens")
                            notificar_paso("gemini_analysis", "✅", "Listo", "#10b981")
                            break
                        except (ProcesoCanceladoException, KeyboardInterrupt):
                            raise
                        except Exception as e:
                            ultimo_error = str(e)
                            error_str = ultimo_error.lower()
                            print(f"   Intento {intento}/{intentos_modelo} con {modelo_actual} (Clave #{num_clave}): {ultimo_error[:120]}")

                            if config_gen and hasattr(config_gen, "thinking_config") and config_gen.thinking_config:
                                print(f"   ⚠️ Servidor de razonamiento sobrecargado en {modelo_actual}. Reintentando de inmediato en modo estándar nativo...")
                                config_gen = types.GenerateContentConfig(max_output_tokens=8192, temperature=0.2)
                                continue

                            if config_gen and any(k in error_str for k in ["thinking", "unrecognized field", "invalid argument"]):
                                config_gen = types.GenerateContentConfig(max_output_tokens=8192, temperature=0.2)
                                print("   Ajustando configuración de razonamiento para compatibilidad...")
                                continue

                            # Detectar límite de cuota (429), clave inválida/revocada o error de autenticación (401/403)
                            es_rotacion_gen = any(term in error_str for term in [
                                "429", "resource_exhausted", "quota", "401", "unauthenticated",
                                "invalid", "unsupported", "permission", "403"
                            ])
                            if es_rotacion_gen:
                                gestor_cuotas.registrar_429(modelo_actual, ultimo_error)
                                print(f"\n🔄 [FALLO O CUOTA EN CLAVE #{num_clave}] Conmutando automáticamente a la siguiente API Key del pool...")
                                notificar_paso("gemini_analysis", "🔄", f"Clave #{num_clave} no disponible. Rotando...", "#f59e0b")
                                gestor_pool.rotar_siguiente_clave(motivo=f"Error/Cuota con {modelo_actual}: {ultimo_error[:80]}")
                                hubo_429 = True
                                try:
                                    client.files.delete(name=gemini_file.name)
                                except Exception:
                                    pass
                                gemini_file = None
                                break

                            es_saturacion = any(term in error_str for term in [
                                "503", "unavailable", "high demand", "504", "deadline",
                                "timeout", "timed out", "overloaded", "server error", "spikes"
                            ])

                            if es_saturacion:
                                if intento < intentos_modelo:
                                    tiempo_espera = 4 * intento
                                    print(f"   ⏳ Google experimenta pico de demanda temporal (503). Reintentando en {tiempo_espera}s ({intento}/{intentos_modelo})...")
                                    notificar_paso("gemini_analysis", "⏳", f"Reintentando {modelo_actual} ({intento}/{intentos_modelo})...", "#f59e0b")
                                    dormir_con_escape(tiempo_espera)
                                    continue
                                else:
                                    gestor_cuotas.registrar_503(modelo_actual, ultimo_error)
                                    print(f"   Servidores saturados con {modelo_actual} (503) tras {intentos_modelo} intentos. Conmutando al siguiente modelo...")
                                    config_gen = types.GenerateContentConfig(max_output_tokens=8192, temperature=0.2)
                                    break

                            if intento < intentos_modelo:
                                tiempo_espera = 4 * intento
                                print(f"   Reintentando con {modelo_actual} en {tiempo_espera}s...")
                                notificar_paso("gemini_analysis", "⏳", f"Reintentando {modelo_actual} ({intento}/{intentos_modelo})...", "#f59e0b")
                                dormir_con_escape(tiempo_espera)
                            else:
                                print(f"   Agotados los {intentos_modelo} intentos con {modelo_actual}.")

                if response is not None:
                    break

            if response is None:
                if _cancelacion_solicitada:
                    _cancelacion_solicitada = False
                    raise ProcesoCanceladoException("Operación cancelada por el usuario.")
                raise RuntimeError(f"No fue posible conectar con Gemini tras evaluar las API Keys del pool: {ultimo_error}")

            if not response or not hasattr(response, "text") or not response.text:
                raise RuntimeError("No se obtuvo una respuesta de texto válida de Gemini.")

            reporte_crudo = response.text.strip()
            titulo_tarea, cuerpo_reporte = extraer_titulo_y_cuerpo(reporte_crudo, version_app)

        # Sanitización universal e incondicional del título para evitar artefactos o etiquetas duplicadas
        titulo_tarea = sanitizar_titulo(titulo_tarea, version_app)

        # Nombre limpio para archivo
        nombre_limpio_archivo = limpiar_nombre_archivo(titulo_tarea)
        if not nombre_limpio_archivo or len(nombre_limpio_archivo) < 3:
            nombre_limpio_archivo = f"[{version_app}] Reporte de error detectado en QA"

        print("\n" + "-" * 50)
        print(f"📌 TÍTULO GENERADO:\n{titulo_tarea}")
        print("-" * 50)
        print(f"📋 VISTA PREVIA DEL REPORTE:\n{cuerpo_reporte[:350]}...\n" + "-" * 50)

        # Renombrar archivo local si está habilitado
        if renombrar_local:
            directorio_archivo = os.path.dirname(archivo_path)
            nuevo_path_local = os.path.join(directorio_archivo, f"{nombre_limpio_archivo}{ext}")
            if os.path.abspath(archivo_path) != os.path.abspath(nuevo_path_local):
                try:
                    if not os.path.exists(nuevo_path_local):
                        os.rename(archivo_path, nuevo_path_local)
                        archivo_actual_path = nuevo_path_local
                        print(f"\n📂 Archivo local renombrado con éxito a:\n   {nuevo_path_local}")
                    else:
                        archivo_actual_path = nuevo_path_local
                except Exception as e:
                    print(f"   ⚠️ No se pudo renombrar el archivo local: {e}")

        # 3. Subir a Google Drive resolviendo la carpeta inteligente (Standard, Air, Backend, KDS, etc.)
        notificar_paso("drive_upload", "⏳", "Resolviendo carpeta...", "#3b82f6")
        print(f"\n[3/4] ☁️ Subiendo {tipo_texto} a Google Drive...")
        service_drive = autenticar_drive()

        producto_detectado = determinar_producto(
            etiquetas=etiquetas,
            descripcion=descripcion_problema,
            titulo=titulo_tarea,
            producto_forzado=modulo_producto
        )
        folder_id_destino, ruta_drive = resolver_carpeta_destino(
            service=service_drive,
            producto=producto_detectado,
            version_app=version_app
        )
        print(f"   📂 Destino en Drive: {ruta_drive} (ID: {folder_id_destino})")
        notificar_paso("drive_upload", "⏳", "Subiendo a Drive...", "#3b82f6")

        drive_link, drive_file_id = subir_a_drive(service_drive, archivo_actual_path, nombre_limpio_archivo, folder_id_destino)
        print(f"   🔗 Enlace de Drive generado: {drive_link}")
        notificar_paso("drive_upload", "✅", "Listo", "#10b981")

        # Formatear evidencias adjuntas en Todoist
        nombre_con_ext = f"{nombre_limpio_archivo}{ext}"
        evidencia_md = f"[{nombre_con_ext}]({drive_link})"
        
        patron_evidencias = re.compile(r"(\*{0,2}Evidencias adjuntas:\*{0,2}).*", flags=re.IGNORECASE | re.DOTALL)
        if patron_evidencias.search(cuerpo_reporte):
            cuerpo_con_evidencia = patron_evidencias.sub(r"\1\n" + evidencia_md, cuerpo_reporte)
        else:
            cuerpo_con_evidencia = f"{cuerpo_reporte}\n\nEvidencias adjuntas:\n{evidencia_md}"

        # Prioridad Todoist
        mapa_prioridades = {1: 4, 2: 3, 3: 2, 4: 1}
        prioridad_api = mapa_prioridades.get(prioridad_ui, 2)

        # 4. Crear tarea en Todoist
        notificar_paso("todoist_create", "⏳", "Creando tarea...", "#3b82f6")
        print(f"\n[4/4] 📝 Creando tarea en Todoist (Proyecto '{PROJECT_NAME}', Sección '{SECTION_NAME}')...")
        p_id, s_id = obtener_ids_todoist()
        tarea = crear_tarea_todoist(
            titulo=titulo_tarea,
            descripcion=cuerpo_con_evidencia,
            project_id=p_id,
            section_id=s_id,
            prioridad=prioridad_api,
            labels=etiquetas
        )
        task_id = tarea.get("id")
        notificar_paso("todoist_create", "✅", "Publicado", "#10b981")

        # Formatear enlace de Todoist idéntico a la app
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', titulo_tarea.lower()).strip('-')[:75]
        task_url = f"https://app.todoist.com/app/task/{slug}-{task_id}"

        # Mensaje exacto para WhatsApp
        mensaje_whatsapp = formatear_mensaje_whatsapp(
            titulo=titulo_tarea,
            task_url=task_url,
            drive_link=drive_link,
            es_imagen=es_imagen,
            version_app=version_app
        )

        # Copiar automáticamente al portapapeles de Windows
        copiar_al_portapapeles(mensaje_whatsapp)

        print("\n" + "=" * 60)
        print("🎉 ¡ÉXITO TOTAL! REPORTE SUBIDO Y TICKET CREADO")
        print("=" * 60)
        ubicacion_str = f"{PROJECT_NAME} -> {SECTION_NAME}" if not tarea.get("_inbox_fallback") else "Bandeja de Entrada (Inbox) [OTF Development lleno]"
        print(f"📌 Título en Todoist:   {titulo_tarea}")
        print(f"📍 Proyecto / Sección:  {ubicacion_str}")
        print(f"🏷️  Etiquetas aplicadas: {', '.join(etiquetas)}")
        print(f"🔗 Enlace directo:      {task_url}")
        print(f"📎 Enlace Drive:        {drive_link}")
        if tarea.get("_inbox_fallback"):
            print("⚠️  AVISO: Guardado en tu Inbox porque 'OTF Development' tiene 305/300 tareas activas.")
        print("=" * 60)

        # Cuadro especial para WhatsApp
        print("\n" + "💬 " + "=" * 56 + " 💬")
        print("📋 MENSAJE LISTO PARA PEGAR EN WHATSAPP:")
        print("-" * 60)
        print(mensaje_whatsapp)
        print("-" * 60)
        print("✨ ¡Copiado automáticamente al portapapeles de Windows!")
        print("👉 Solo abre WhatsApp y presiona Ctrl + V para enviarlo.")
        print("💬 " + "=" * 56 + " 💬\n")

        # Ventana de 10 segundos para revertir en caso de arrepentirse (solo para consola interactiva)
        revertir = False
        if not hook_cambio_paso:
            print("=" * 60)
            print("💡 ¿Te arrepentiste de enviar este reporte? Tienes 10s para presionar [ESC] y revertir...")
            print("👉 (O presiona [Enter] / cualquier otra tecla para confirmar definitivamente)")
            print("=" * 60)

            fin_espera = time.time() + 10
            while time.time() < fin_espera:
                tiempo_restante = int(fin_espera - time.time()) + 1
                sys.stdout.write(f"\r   ⏳ Tiempo para revertir con tecla ESC: {tiempo_restante}s   ")
                sys.stdout.flush()
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == '\x1b':
                        revertir = True
                        break
                    elif ch in ('\r', '\n', ' '):
                        break
                time.sleep(0.05)

            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()

        if revertir:
            revertir_acciones(
                ruta_original=archivo_path,
                ruta_renombrada=archivo_actual_path,
                drive_service=service_drive,
                drive_file_id=drive_file_id,
                task_id=task_id
            )
            return None
        else:
            print("✅ Reporte confirmado y guardado con éxito.\n")
            return {
                "tarea_id": task_id,
                "titulo": titulo_tarea,
                "task_url": task_url,
                "drive_link": drive_link,
                "ruta_drive": ruta_drive,
                "producto_drive": producto_detectado,
                "mensaje_whatsapp": mensaje_whatsapp,
                "nombre_archivo": nombre_limpio_archivo,
                "archivo_final": archivo_actual_path,
                "drive_service": service_drive,
                "drive_file_id": drive_file_id,
                "ruta_original": archivo_path,
                "es_imagen": es_imagen,
                "version_app": version_app
            }

    except (ProcesoCanceladoException, KeyboardInterrupt):
        revertir_acciones(
            ruta_original=archivo_path,
            ruta_renombrada=archivo_actual_path,
            drive_service=service_drive,
            drive_file_id=drive_file_id,
            task_id=task_id
        )
        raise
    finally:
        if 'client' in locals() and client and 'gemini_file' in locals() and gemini_file:
            try:
                client.files.delete(name=gemini_file.name)
            except Exception:
                pass


def procesar_video(video_path: str, version_app: str = "4.4.1.08debug", dispositivo: str = "E800",
                   prioridad_ui: int = 3, etiquetas: list = None, renombrar_local: bool = True,
                   descripcion_problema: str = "", modulo_producto: str = "Auto"):
    """Función de compatibilidad que redirige a procesar_evidencia."""
    return procesar_evidencia(
        archivo_path=video_path,
        version_app=version_app,
        dispositivo=dispositivo,
        prioridad_ui=prioridad_ui,
        etiquetas=etiquetas,
        renombrar_local=renombrar_local,
        descripcion_problema=descripcion_problema,
        modulo_producto=modulo_producto
    )


# ==========================================
# 6. INTERFAZ DE CONSOLA INTERACTIVA
# ==========================================
def seleccionar_evidencia_interactiva() -> str:
    """Muestra los videos e imágenes encontrados en la carpeta por defecto o solicita la ruta."""
    print("\n🔍 Buscando grabaciones y capturas en:")
    print(f"   {VIDEOS_DEFAULT_DIR}")

    archivos_disponibles = []
    if os.path.exists(VIDEOS_DEFAULT_DIR):
        todos = os.listdir(VIDEOS_DEFAULT_DIR)
        archivos_disponibles = [
            os.path.join(VIDEOS_DEFAULT_DIR, f)
            for f in todos
            if os.path.splitext(f)[1].lower() in EXTENSIONES_VALIDAS
        ]
        archivos_disponibles.sort(key=lambda x: os.path.getmtime(x), reverse=True)

    if archivos_disponibles:
        print("\nEvidencias encontradas (ordenadas por la más reciente):")
        for idx, a_path in enumerate(archivos_disponibles[:10], 1):
            nombre = os.path.basename(a_path)
            tam_bytes = os.path.getsize(a_path)
            tam_str = f"{tam_bytes / (1024 * 1024):.1f} MB" if tam_bytes >= 1024 * 1024 else f"{tam_bytes / 1024:.1f} KB"
            ext = os.path.splitext(a_path)[1].lower()
            icono = "🖼️" if ext in EXTENSIONES_IMAGEN else "📹"
            print(f"  [{idx}] {icono} {nombre} ({tam_str})")
        print("  [O] Ingresar otra ruta de archivo manualmente")

        seleccion = leer_input_con_escape(f"\nSelecciona un número [Por defecto: 1, ESC para salir]: ").strip()
        if not seleccion or seleccion == "1":
            return archivos_disponibles[0]
        elif seleccion.isdigit() and 1 <= int(seleccion) <= len(archivos_disponibles):
            return archivos_disponibles[int(seleccion) - 1]

    while True:
        ruta = leer_input_con_escape("\nIntroduce o arrastra la ruta completa del video o imagen (.mp4, .png, .jpg) [ESC para salir]: ").strip().strip('"').strip("'")
        if not ruta:
            continue
        if os.path.isfile(ruta):
            ext = os.path.splitext(ruta)[1].lower()
            if ext in EXTENSIONES_VALIDAS:
                return ruta
            print(f"❌ Formato no soportado ({ext}). Formatos válidos: {', '.join(EXTENSIONES_VALIDAS)}")
        else:
            print("❌ Ruta inválida o archivo no encontrado. Intenta de nuevo.")


def seleccionar_video_interactivo() -> str:
    """Función de compatibilidad."""
    return seleccionar_evidencia_interactiva()


def seleccionar_etiquetas_interactivas() -> list:
    """Permite seleccionar de forma interactiva una o varias etiquetas para el reporte."""
    print("\n" + "-" * 50)
    print("🏷️  SELECCIÓN DE ETIQUETAS:")
    print("-" * 50)
    for idx, et in enumerate(ETIQUETAS_DISPONIBLES, 1):
        # Destacar las habituales
        marca = " (predeterminada)" if et in ("Bug", "Apk Standard", "Offline") else ""
        print(f"  [{idx:2d}] {et}{marca}")
    print("  [ C] Ingresar etiquetas personalizadas manualmente")
    print("-" * 50)

    seleccion = leer_input_con_escape("Elige los números separados por coma [Enter para 1, 2, 3]: ").strip()

    if not seleccion:
        return ["Bug", "Apk Standard", "Offline"]

    if seleccion.upper() == "C":
        personalizadas = leer_input_con_escape("Introduce las etiquetas separadas por coma: ").strip()
        return [e.strip() for e in personalizadas.split(",") if e.strip()]

    etiquetas_elegidas = []
    partes = [p.strip() for p in seleccion.split(",") if p.strip()]
    for p in partes:
        if p.isdigit():
            num = int(p)
            if 1 <= num <= len(ETIQUETAS_DISPONIBLES):
                tag = ETIQUETAS_DISPONIBLES[num - 1]
                if tag not in etiquetas_elegidas:
                    etiquetas_elegidas.append(tag)
        else:
            # Si el usuario escribió directamente el nombre de una etiqueta
            if p not in etiquetas_elegidas:
                etiquetas_elegidas.append(p)

    return etiquetas_elegidas if etiquetas_elegidas else ["Bug", "Apk Standard", "Offline"]


def menu_consola():
    """Menú interactivo para ejecutar cómodamente desde la terminal de VS Code."""
    print("\n" + "=" * 60)
    print("🛠️  SUBIR REPORTE DE QA AUTOMATIZADO A TODOIST")
    print("💡 (Presiona [ESC] en cualquier momento para cancelar o revertir)")
    print("=" * 60)

    # 1. Seleccionar archivo de evidencia (video o imagen)
    archivo_path = seleccionar_evidencia_interactiva()
    ext = os.path.splitext(archivo_path)[1].lower()
    es_imagen = ext in EXTENSIONES_IMAGEN

    # 2. Exposición textual del problema (obligatoria si es imagen, opcional si es video)
    descripcion_problema = ""
    if es_imagen:
        print("\n" + "-" * 50)
        print("📝 EXPOSICIÓN TEXTUAL DEL PROBLEMA:")
        print("Explica detalladamente qué ocurre, qué botón/pantalla falló o el comportamiento anómalo:")
        print("-" * 50)
        while not descripcion_problema:
            descripcion_problema = leer_input_con_escape("Describe el problema observado [ESC para salir]: ").strip()
            if not descripcion_problema:
                print("⚠️ Para reportes basados en imágenes es necesario ingresar una descripción del bug.")
    else:
        desc_opc = leer_input_con_escape("\nDescripción o notas adicionales del bug (opcional) [Enter para omitir]: ").strip()
        descripcion_problema = desc_opc

    # 3. Versión de la app
    version_input = leer_input_con_escape("\nVersión de la App [Enter para '4.4.1.08debug']: ").strip()
    version = version_input if version_input else "4.4.1.08debug"

    # 4. Dispositivo
    disp_input = leer_input_con_escape("Dispositivo utilizado [Enter para 'E800']: ").strip()
    dispositivo = disp_input if disp_input else "E800"

    # 5. Selección detallada de etiquetas
    etiquetas = seleccionar_etiquetas_interactivas()
    print(f"   ✅ Etiquetas seleccionadas: {', '.join(etiquetas)}")

    # 6. Prioridad Todoist (1 a 4)
    prio_input = leer_input_con_escape("\nPrioridad en Todoist (1=Urgente P1, 2=Alta P2, 3=Media P3, 4=Baja P4) [Enter para 3]: ").strip()
    prioridad = int(prio_input) if prio_input.isdigit() and int(prio_input) in (1, 2, 3, 4) else 3

    # Ejecutar proceso
    procesar_evidencia(
        archivo_path=archivo_path,
        version_app=version,
        dispositivo=dispositivo,
        prioridad_ui=prioridad,
        etiquetas=etiquetas,
        renombrar_local=True,
        descripcion_problema=descripcion_problema,
        modelo_gemini="gemini-3.8-flash"
    )


# ==========================================
# 7. PUNTO DE ENTRADA
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automatización de reportes de QA a Todoist y Drive")
    parser.add_argument("--archivo", "--video", "--imagen", dest="archivo", type=str, help="Ruta completa al archivo (.mp4, .png, .jpg, .jpeg)")
    parser.add_argument("--version", type=str, default="4.4.1.08debug", help="Versión de la app (ej: 4.4.1.08debug)")
    parser.add_argument("--device", type=str, default="E800", help="Dispositivo (ej: E800, PAX A920Pro)")
    parser.add_argument("--priority", type=int, default=3, choices=[1, 2, 3, 4], help="Prioridad en UI Todoist (1-4)")
    parser.add_argument("--labels", type=str, default="Bug,Apk Standard,Offline", help="Etiquetas separadas por coma")
    parser.add_argument("--descripcion", "--problema", dest="descripcion", type=str, default="", help="Descripción textual del problema")
    parser.add_argument("--model", "--modelo", dest="model", type=str, default="gemini-3.8-flash", help="Modelo de Gemini a utilizar (ej: gemini-3.8-flash, gemini-3.6-flash)")
    parser.add_argument("--no-thinking", dest="thinking", action="store_false", default=True, help="Desactivar razonamiento ampliado (thinking)")
    parser.add_argument("--cli", "--consola", action="store_true", help="Forzar ejecución en consola en lugar de la ventana modal")

    args = parser.parse_args()

    try:
        if args.archivo:
            labels_list = [l.strip() for l in args.labels.split(",") if l.strip()]
            procesar_evidencia(
                archivo_path=args.archivo,
                version_app=args.version,
                dispositivo=args.device,
                prioridad_ui=args.priority,
                etiquetas=labels_list,
                renombrar_local=True,
                descripcion_problema=args.descripcion,
                modelo_gemini=args.model,
                razonamiento_ampliado=args.thinking
            )
        elif args.cli:
            menu_consola()
        else:
            try:
                from gui_subir_reporte import abrir_modal_gui
                abrir_modal_gui()
            except Exception as e:
                print(f"⚠️ No se pudo iniciar el modal gráfico ({e}). Iniciando menú en consola...")
                menu_consola()
    except ProcesoCanceladoException:
        print("\n\n⛔ Proceso cancelado o revertido por el usuario con la tecla ESC. Saliendo limpiamente...")
        sys.exit(0)
    except KeyboardInterrupt:
        print("\n\n⛔ Proceso cancelado por el usuario (Ctrl + C). Saliendo limpiamente...")
        sys.exit(0)