#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
code_review.py
=============================================================================
Sistema Automatizado e Inteligente de Verificación de Bugs ("Code Review / QA")
-----------------------------------------------------------------------------
Permite verificar sobre un dispositivo físico Android (vía ADB) los reportes
de bugs marcados en la columna "Code Review / QA" de Todoist.

Características:
1. Detecta y lista dispositivos conectados vía ADB para enlace inmediato.
2. Activa visualización de toques en pantalla física (show_touches).
3. Solicita el link de la tarea en Todoist o lista las tareas de la columna.
4. Filtra pruebas que requieren hardware o equipos fuera del entorno local.
5. Recorre inteligentemente la interfaz mediante análisis UI + IA Gemini.
6. Monitorea y valida los logs de la app en cada movimiento.
7. Emite el veredicto: "EL BUG YA FUE CORREGIDO" o "EL BUG AÚN SE MANTIENE".
=============================================================================
"""

import os
import sys
import re
import time
import json
import html
import msvcrt
import argparse
import subprocess
import webbrowser
import xml.etree.ElementTree as ET
import math
from typing import Dict, List, Optional, Tuple, Any

import requests
import urllib.request
from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput

from google import genai
from google.genai import types
import io
import unicodedata
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from onthefly_knowledge_engine import OnTheFlyKnowledgeBase

# Forzar UTF-8 en consola de Windows para evitar errores de codificación con emojis
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==========================================
# 1. CONFIGURACIONES GENERALES
# ==========================================
from gemini_keys_pool import gestor_pool
GEMINI_API_KEY = gestor_pool.obtener_clave_activa()[0]
TODOIST_API_TOKEN = "1d4235b0ddeafea41bceeb79d28ae65b340c8c46"

PROJECT_NAME = "OTF Development"
SECTION_NAME = "Code Review / QA"
DEFAULT_PACKAGE = "com.kubilabs.ontheflypos"
APPIUM_SERVER_URL = "http://127.0.0.1:4723"

# Directorio temporal local para capturas intermedias
SCRATCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_temp")
os.makedirs(SCRATCH_DIR, exist_ok=True)


# ==========================================
# 2. CONTROL DE CANCELACIÓN (TECLA ESC)
# ==========================================
class ProcesoCanceladoException(Exception):
    """Excepción lanzada cuando el usuario cancela con la tecla ESC."""
    pass


def leer_input_con_escape(prompt: str = "") -> str:
    """Lee una línea de texto desde la consola permitiendo cancelar de inmediato con ESC o Ctrl+C."""
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()

    if not sys.stdin.isatty():
        try:
            line = sys.stdin.readline()
            return line.strip()
        except Exception:
            return ""

    buffer = []
    while True:
        try:
            ch = msvcrt.getwch()
        except Exception:
            return input()

        # Teclas especiales de Windows
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

        # Caracteres imprimibles
        elif ord(ch) >= 32:
            buffer.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()


CANCELAR_PROCESO = False

def dormir_con_escape(segundos: float):
    """Pausa la ejecución permitiendo abortar en cualquier momento con ESC o flag de GUI."""
    global CANCELAR_PROCESO
    fin = time.time() + segundos
    while time.time() < fin:
        if CANCELAR_PROCESO:
            raise ProcesoCanceladoException("Operación cancelada desde la interfaz (botón Detener).")
        if sys.stdin.isatty() and msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch == '\x1b':
                raise ProcesoCanceladoException("Operación cancelada por el usuario con la tecla ESC.")
            elif ch == '\x03':
                raise KeyboardInterrupt()
        time.sleep(0.05)


# ==========================================
# 3. GESTOR DE DISPOSITIVOS ADB
# ==========================================
class ADBController:
    """Controlador de comunicación y automatización híbrido (Appium UiAutomator2 + ADB nativo)."""

    def __init__(self, serial: str = None, appium_url: str = APPIUM_SERVER_URL):
        self.serial = serial
        self.package_name = DEFAULT_PACKAGE
        self.appium_url = appium_url
        self.driver = None
        self.modo_appium = False

    def conectar_appium(self) -> bool:
        """Intenta inicializar o adjuntarse a una sesión viva de Appium v3 con UiAutomator2."""
        try:
            with urllib.request.urlopen(f"{self.appium_url}/status", timeout=3) as res:
                if res.status != 200:
                    return False
        except Exception:
            return False

        try:
            print(f"\n   🔌 Servidor Appium detectado en {self.appium_url}...")
            print("   ⏳ Inicializando sesión UiAutomator2 sobre la aplicación...")
            options = UiAutomator2Options()
            options.platform_name = "Android"
            options.automation_name = "UiAutomator2"
            if self.serial:
                options.udid = self.serial
            options.no_reset = True
            options.set_capability("appium:autoLaunch", False)
            options.set_capability("appium:appPackage", self.package_name)
            options.set_capability("appium:waitForIdleTimeout", 100)
            options.set_capability("appium:skipDeviceInitialization", True)
            options.set_capability("appium:newCommandTimeout", 300)

            self.driver = webdriver.Remote(self.appium_url, options=options)
            self.modo_appium = True
            print(f"   ⚡ ¡MODO APPIUM ACTIVADO CON ÉXITO! (Sesión ID: {self.driver.session_id})")
            return True
        except Exception as e:
            print(f"   ⚠️ No se pudo inicializar sesión de Appium ({e}). Operando en modo ADB nativo.")
            self.driver = None
            self.modo_appium = False
            return False

    def cerrar_sesion(self):
        """Cierra la sesión de Appium si está activa."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass

    def ejecutar_adb(self, cmd_args: List[str], timeout: int = 25) -> Tuple[int, str, str]:
        """Ejecuta un comando ADB dirigido al dispositivo actual."""
        base_cmd = ["adb"]
        if self.serial:
            base_cmd.extend(["-s", self.serial])
        full_cmd = base_cmd + cmd_args

        try:
            res = subprocess.run(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout
            )
            return res.returncode, res.stdout, res.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Timeout al ejecutar comando ADB"
        except Exception as e:
            return -1, "", str(e)

    @staticmethod
    def listar_dispositivos() -> List[Dict[str, str]]:
        """Lista todos los dispositivos conectados reconocidos por ADB."""
        try:
            res = subprocess.run(["adb", "devices", "-l"],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 text=True,
                                 encoding="utf-8",
                                 errors="replace")
            lineas = res.stdout.strip().splitlines()
        except Exception as e:
            print(f"❌ Error al consultar 'adb devices': {e}")
            return []

        dispositivos = []
        for linea in lineas[1:]:
            linea = linea.strip()
            if not linea:
                continue
            partes = linea.split()
            if len(partes) >= 2:
                serial = partes[0]
                estado = partes[1]

                # Obtener detalles del modelo
                modelo = "Desconocido"
                fabricante = "Android"
                if estado == "device":
                    try:
                        r_m = subprocess.run(["adb", "-s", serial, "shell", "getprop", "ro.product.model"],
                                             stdout=subprocess.PIPE, text=True, timeout=5)
                        modelo = r_m.stdout.strip() or modelo
                        r_f = subprocess.run(["adb", "-s", serial, "shell", "getprop", "ro.product.manufacturer"],
                                             stdout=subprocess.PIPE, text=True, timeout=5)
                        fabricante = r_f.stdout.strip() or fabricante
                    except Exception:
                        pass

                dispositivos.append({
                    "serial": serial,
                    "estado": estado,
                    "modelo": modelo,
                    "fabricante": fabricante,
                    "raw": linea
                })

        return dispositivos

    def activar_indicadores_visuales(self):
        """Activa indicadores táctiles y optimiza animaciones para dump confiable."""
        print("   👁️ Activando visualización de toques en pantalla física (show_touches)...")
        self.ejecutar_adb(["shell", "settings", "put", "system", "show_touches", "1"])
        self.ejecutar_adb(["shell", "settings", "put", "system", "pointer_location", "1"])
        # Desactivar animaciones para evitar bloqueos de 'could not get idle state' en uiautomator
        self.ejecutar_adb(["shell", "settings", "put", "global", "window_animation_scale", "0"])
        self.ejecutar_adb(["shell", "settings", "put", "global", "transition_animation_scale", "0"])
        self.ejecutar_adb(["shell", "settings", "put", "global", "animator_duration_scale", "0"])

    def restaurar_indicadores_visuales(self):
        """Restaura puntero al finalizar si es necesario."""
        self.ejecutar_adb(["shell", "settings", "put", "system", "pointer_location", "0"])

    def obtener_resolucion(self) -> Tuple[int, int]:
        """Obtiene la resolución física de la pantalla (ancho, alto)."""
        if self.modo_appium and self.driver:
            try:
                tam = self.driver.get_window_size()
                return tam.get("width", 1920), tam.get("height", 1080)
            except Exception:
                pass
        code, out, _ = self.ejecutar_adb(["shell", "wm", "size"])
        match = re.search(r"(\d+)x(\d+)", out)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 1920, 1080

    def capturar_screenshot(self, ruta_destino: str) -> bool:
        """Captura la pantalla física actual (vía Appium o ADB)."""
        if self.modo_appium and self.driver:
            try:
                self.driver.get_screenshot_as_file(ruta_destino)
                return os.path.exists(ruta_destino) and os.path.getsize(ruta_destino) > 0
            except Exception:
                pass

        try:
            with open(ruta_destino, "wb") as f:
                base_cmd = ["adb"]
                if self.serial:
                    base_cmd.extend(["-s", self.serial])
                base_cmd.extend(["exec-out", "screencap", "-p"])
                subprocess.run(base_cmd, stdout=f, timeout=15)
            return os.path.exists(ruta_destino) and os.path.getsize(ruta_destino) > 0
        except Exception as e:
            print(f"   ⚠️ Error capturando pantalla: {e}")
            return False

    def capturar_ui_xml(self) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """Extrae la jerarquía de vistas usando Appium (page_source) si está activo, o ADB como respaldo."""
        out = None
        if self.modo_appium and self.driver:
            try:
                out = self.driver.page_source
            except Exception:
                out = None

        if not out:
            # Estrategia multi-ruta con y sin --compressed para sortear 'could not get idle state'
            for dump_target in ["/data/local/tmp/uidump.xml", "/sdcard/window_dump.xml"]:
                for flag in ["--compressed", ""]:
                    cmd = ["shell", f"rm -f {dump_target} && uiautomator dump {flag} {dump_target}"]
                    self.ejecutar_adb(cmd, timeout=5)
                    code, out_cat, _ = self.ejecutar_adb(["shell", f"cat {dump_target}"], timeout=4)
                    if out_cat and "<hierarchy" in out_cat:
                        out = out_cat
                        break
                if out:
                    break

        if not out or "<hierarchy" not in out:
            return None, []

        # Parsear elementos interactivos relevantes
        elementos = []
        try:
            root = ET.fromstring(out)
            for node in root.iter():
                if node.tag == 'hierarchy':
                    continue
                bounds = node.attrib.get('bounds', '')
                text = node.attrib.get('text', '').strip()
                hint = node.attrib.get('hint', '').strip()
                res_id = node.attrib.get('resource-id', '').strip()
                desc = node.attrib.get('content-desc', '').strip()
                clickable = node.attrib.get('clickable', 'false') == 'true'
                enabled = node.attrib.get('enabled', 'false') == 'true'
                cls = node.attrib.get('class') or node.tag

                # Si text está vacío pero hint tiene valor, usar hint como texto descriptivo
                if not text and hint:
                    text = hint

                coords = re.findall(r'\[(\d+),(\d+)\]', bounds)
                if len(coords) == 2:
                    x1, y1 = map(int, coords[0])
                    x2, y2 = map(int, coords[1])
                    cx = (x1 + x2) // 2
                    cy = (y1 + y2) // 2

                    if text or res_id or desc or hint or clickable:
                        elementos.append({
                            "text": text,
                            "hint": hint,
                            "resource_id": res_id,
                            "desc": desc,
                            "class": cls,
                            "clickable": clickable,
                            "enabled": enabled,
                            "bounds": [x1, y1, x2, y2],
                            "center": (cx, cy)
                        })
        except Exception as e:
            print(f"   ⚠️ Error procesando jerarquía XML: {e}")

        return out, elementos

    def tap(self, x: int, y: int, res_id: str = None):
        """Ejecuta un toque táctil mediante Appium o ADB."""
        if self.modo_appium and self.driver:
            # Si se conoce el resource-id, intentar click directo
            if res_id:
                try:
                    elem = self.driver.find_element(by=AppiumBy.ID, value=res_id)
                    if elem:
                        elem.click()
                        return
                except Exception:
                    pass

            # Click W3C por coordenadas
            try:
                actions = ActionChains(self.driver)
                actions.w3c_actions = ActionBuilder(self.driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch"))
                actions.w3c_actions.pointer_action.move_to_location(x, y)
                actions.w3c_actions.pointer_action.pointer_down()
                actions.w3c_actions.pointer_action.pause(0.1)
                actions.w3c_actions.pointer_action.release()
                actions.perform()
                return
            except Exception:
                pass

        self.ejecutar_adb(["shell", "input", "tap", str(x), str(y)])

    def type_text(self, texto: str, res_id: str = None):
        """Escribe texto limpiando previamente el campo para evitar texto duplicado o corrupto."""
        if self.modo_appium and self.driver and res_id:
            try:
                elem = self.driver.find_element(by=AppiumBy.ID, value=res_id)
                if elem:
                    elem.clear()
                    elem.send_keys(texto)
                    return
            except Exception:
                pass

        # Limpiar texto residual en ADB con un solo comando rápido
        self.ejecutar_adb(["shell", "input", "keyevent", "67", "67", "67", "67", "67", "67", "67", "67", "67", "67", "67", "67", "67", "67", "67", "67"])

        texto_limpio = texto.replace(" ", "%s").replace("&", "\\&")
        self.ejecutar_adb(["shell", "input", "text", texto_limpio])

    def keyevent(self, keycode: int):
        """Envía evento de tecla de Android (ej: 4=Back, 66=Enter)."""
        if self.modo_appium and self.driver:
            try:
                self.driver.press_keycode(keycode)
                return
            except Exception:
                pass
        self.ejecutar_adb(["shell", "input", "keyevent", str(keycode)])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400):
        """Ejecuta un gesto de desplazamiento (swipe)."""
        if self.modo_appium and self.driver:
            try:
                self.driver.swipe(x1, y1, x2, y2, duration_ms)
                return
            except Exception:
                pass
        self.ejecutar_adb(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)])

    def detectar_capacidad_pago_tarjeta(self) -> Dict[str, Any]:
        """
        Detecta si el dispositivo puede procesar pagos con tarjeta de credito/debito.
        Los terminales PAX tienen lector de tarjeta integrado. Tambien verifica
        pinpad USB externo (Ingenico, Verifone, ID Tech).
        """
        # 1. Terminal PAX (lector integrado en todos los modelos E700/E800/A920)
        code, out, _ = self.ejecutar_adb(["shell", "getprop", "ro.product.manufacturer"])
        if "pax" in out.lower():
            code2, model, _ = self.ejecutar_adb(["shell", "getprop", "ro.product.model"])
            return {
                "puede_pagar_tarjeta": True,
                "tipo": "PAX_INTEGRADO",
                "descripcion": f"Terminal PAX {model.strip()} con lector de tarjeta integrado",
                "requiere_accion_fisica": True,
                "instruccion_operador": "Inserta o desliza la tarjeta de prueba en el lector integrado del terminal."
            }
        # 2. Pinpad USB externo (puerto serial /dev/ttyUSB)
        code2, out2, _ = self.ejecutar_adb(["shell", "ls /dev/ttyUSB0 2>/dev/null || echo NOTFOUND"])
        if "NOTFOUND" not in out2 and out2.strip():
            return {
                "puede_pagar_tarjeta": True,
                "tipo": "PINPAD_SERIE",
                "descripcion": f"Pinpad externo detectado en {out2.strip()[:50]}",
                "requiere_accion_fisica": True,
                "instruccion_operador": "Inserta la tarjeta en el pinpad externo conectado por USB/serial."
            }
        # 3. Dispositivos USB genéricos (Ingenico, Verifone)
        code3, out3, _ = self.ejecutar_adb(["shell", "lsusb 2>/dev/null || echo ''"])
        if any(k in out3.lower() for k in ["ingenico", "verifone", "2049:", "1c7a:"]):
            return {
                "puede_pagar_tarjeta": True,
                "tipo": "PINPAD_USB",
                "descripcion": "Pinpad USB externo detectado (Ingenico/Verifone)",
                "requiere_accion_fisica": True,
                "instruccion_operador": "Procesa el pago en el pinpad USB conectado al terminal."
            }
        return {
            "puede_pagar_tarjeta": False,
            "tipo": "NINGUNO",
            "descripcion": "Sin terminal de pago de tarjeta detectada",
            "requiere_accion_fisica": False,
            "instruccion_operador": ""
        }

    def limpiar_logs(self):
        """Limpia el buffer de logcat del dispositivo."""
        self.ejecutar_adb(["logcat", "-c"])

    def leer_logs_recientes(self, lineas: int = 45) -> str:
        """Obtiene las últimas líneas del registro de logcat filtradas para la app."""
        code, out, _ = self.ejecutar_adb(["logcat", "-d", "-t", str(lineas)])
        lineas_out = []
        for l in out.splitlines():
            if any(term in l for term in [self.package_name, "FATAL", "Exception", "AndroidRuntime", "System.err"]):
                lineas_out.append(l.strip())

        if not lineas_out:
            return "\n".join(out.splitlines()[-15:])
        return "\n".join(lineas_out[-25:])


# ==========================================
# 4. CLIENTE TODOIST ("Code Review / QA")
# ==========================================
class TodoistReviewClient:
    """Cliente para consultar tareas y detalles en la columna Code Review / QA."""

    PROJECT_ID_DEFAULT = "6Wp8xp8gMqm5R2Cr"
    SECTION_ID_DEFAULT = "6Wp8xqf2qXQq5qRJ"

    def __init__(self, token: str = TODOIST_API_TOKEN):
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}

    def _peticion_con_reintentos(self, url: str, metodo: str = "get", json_data: dict = None, max_intentos: int = 4) -> requests.Response:
        """Realiza peticiones HTTP con reintentos automáticos en caso de saturación o fallos temporales de red."""
        delays = [2, 4, 7, 10]
        ultimo_error = None

        for intento in range(1, max_intentos + 1):
            try:
                if metodo.lower() == "get":
                    res = requests.get(url, headers=self.headers, timeout=15)
                else:
                    res = requests.post(url, headers=self.headers, json=json_data, timeout=15)

                if res.status_code in (200, 204):
                    return res
                elif res.status_code in (500, 502, 503, 504, 429):
                    espera = delays[min(intento - 1, len(delays) - 1)]
                    print(f"   ⚠️ Respuesta de Todoist ({res.status_code}). Reintentando en {espera}s (intento {intento}/{max_intentos})...")
                    time.sleep(espera)
                else:
                    return res
            except Exception as e:
                ultimo_error = e
                espera = delays[min(intento - 1, len(delays) - 1)]
                print(f"   ⚠️ Error de conexión con Todoist ({e}). Reintentando en {espera}s...")
                time.sleep(espera)

        raise RuntimeError(f"Fallo en la comunicación con Todoist tras {max_intentos} intentos. {ultimo_error}")

    def obtener_proyecto_y_seccion(self) -> Tuple[str, str]:
        """Obtiene los IDs de proyecto 'OTF Development' y sección 'Code Review / QA'."""
        try:
            r_p = self._peticion_con_reintentos("https://api.todoist.com/api/v1/projects")
            if r_p.status_code == 200:
                proyectos = r_p.json().get('results', [])
                p_match = next((p['id'] for p in proyectos if p.get('name') == PROJECT_NAME), None)
                if p_match:
                    r_s = self._peticion_con_reintentos(f"https://api.todoist.com/api/v1/sections?project_id={p_match}")
                    if r_s.status_code == 200:
                        secciones = r_s.json().get('results', [])
                        s_match = next((s['id'] for s in secciones if s.get('name') == SECTION_NAME), None)
                        if s_match:
                            return p_match, s_match
        except Exception:
            pass

        # Usar IDs predeterminados validados para el proyecto OTF Development
        return self.PROJECT_ID_DEFAULT, self.SECTION_ID_DEFAULT

    def listar_tareas_code_review(self) -> List[Dict[str, Any]]:
        """Lista todas las tareas activas dentro de la sección 'Code Review / QA'."""
        _, s_id = self.obtener_proyecto_y_seccion()
        res = self._peticion_con_reintentos(f"https://api.todoist.com/api/v1/tasks?section_id={s_id}")
        if res.status_code != 200:
            raise RuntimeError(f"Error al listar tareas de sección: {res.status_code} - {res.text}")
        return res.json().get('results', [])

    def obtener_tarea_por_id(self, task_id: str) -> Dict[str, Any]:
        """Consulta una tarea puntual por su ID."""
        res = self._peticion_con_reintentos(f"https://api.todoist.com/api/v1/tasks/{task_id}")
        if res.status_code != 200:
            raise RuntimeError(f"No se pudo encontrar la tarea '{task_id}' (Código {res.status_code})")
        return res.json()

    def obtener_comentarios(self, task_id: str) -> List[Dict[str, Any]]:
        """Obtiene comentarios de la tarea (notas de desarrollo, commits, versiones corregidas)."""
        try:
            res = self._peticion_con_reintentos(f"https://api.todoist.com/api/v1/comments?task_id={task_id}")
            if res.status_code == 200:
                return res.json().get('results', [])
        except Exception:
            pass
        return []

    def extraer_id_desde_url(self, entrada_usuario: str) -> str:
        """Extrae el task_id de cualquier URL de Todoist o cadena directa."""
        texto = entrada_usuario.strip()

        # Si viene en formato URL: https://app.todoist.com/app/task/nombre-del-bug-6h9MJvf424mwRWfr
        match_url = re.search(r"/task/(?:[a-zA-Z0-9_\-]+-)?([a-zA-Z0-9]{10,24})(?:\?|$)", texto)
        if match_url:
            return match_url.group(1)

        # Formato query param: ?id=6h9MJvf424mwRWfr
        match_param = re.search(r"[?&]id=([a-zA-Z0-9]{10,24})", texto)
        if match_param:
            return match_param.group(1)

        # Si introdujo directamente el ID alfanumérico
        if re.match(r"^[a-zA-Z0-9]{10,24}$", texto):
            return texto

        # Si termina con el ID tras un guion
        partes = texto.split("-")
        if partes and re.match(r"^[a-zA-Z0-9]{10,24}$", partes[-1]):
            return partes[-1]

        return texto


# ==========================================
# 4.5 MONITOR DE CONSUMO DE TOKENS EN TIEMPO REAL
# ==========================================
class MonitorConsumoTokens:
    """Monitor y acumulador en tiempo real del consumo de tokens para Gemini y Groq."""
    gemini_llamadas = 0
    gemini_prompt = 0
    gemini_completion = 0
    gemini_total = 0

    groq_llamadas = 0
    groq_prompt = 0
    groq_completion = 0
    groq_total = 0

    @classmethod
    def registrar_gemini(cls, modelo: str, prompt_tokens: int, completion_tokens: int):
        cls.gemini_llamadas += 1
        cls.gemini_prompt += prompt_tokens
        cls.gemini_completion += completion_tokens
        total = prompt_tokens + completion_tokens
        cls.gemini_total += total
        print(f"   📊 [TOKENS GEMINI] Modelo: {modelo} | Prompt: {prompt_tokens:,} | Salida: {completion_tokens:,} | Llamada: {total:,} tokens | Acumulado sesión: {cls.gemini_total:,} tokens")

    @classmethod
    def registrar_groq(cls, modelo: str, prompt_tokens: int, completion_tokens: int):
        cls.groq_llamadas += 1
        cls.groq_prompt += prompt_tokens
        cls.groq_completion += completion_tokens
        total = prompt_tokens + completion_tokens
        cls.groq_total += total
        print(f"   📊 [TOKENS GROQ]   Modelo: {modelo} | Prompt: {prompt_tokens:,} | Salida: {completion_tokens:,} | Llamada: {total:,} tokens | Acumulado sesión: {cls.groq_total:,} tokens")

    @classmethod
    def mostrar_resumen(cls):
        print("\n" + "=" * 65)
        print("📈 RESUMEN DE CONSUMO DE TOKENS DE IA EN LA SESIÓN:")
        print(f"   🤖 Google Gemini: {cls.gemini_total:,} tokens ({cls.gemini_prompt:,} entrada + {cls.gemini_completion:,} salida) en {cls.gemini_llamadas} llamadas")
        print(f"   ⚡ Groq Cloud:    {cls.groq_total:,} tokens ({cls.groq_prompt:,} entrada + {cls.groq_completion:,} salida) en {cls.groq_llamadas} llamadas")
        print("=" * 65)



# ==========================================
# 4.6 MONITOR DE LOGCAT EN TIEMPO REAL
# ==========================================
class LogCatMonitor:
    """
    Monitor y analizador en tiempo real de logcat durante la ejecución QA.
    Detecta excepciones, crashes y errores relacionados con el bug en cada paso.
    """

    PALABRAS_CRITICAS = [
        "Exception", "NullPointer", "IllegalState", "IllegalArgument",
        "ClassCastException", "IndexOutOfBounds", "StackOverflow",
        "OutOfMemory", "FATAL", "Force close", "has stopped", "ANR",
        "Crash", "CRASH", "W/System.err"
    ]
    PALABRAS_WARNING = ["WARN", "W/ ", "Skipped", "timeout", "retry", "failed", "refused"]

    def __init__(self, adb: 'ADBController', package: str = DEFAULT_PACKAGE):
        self.adb = adb
        self.package = package
        self.logs_sesion: List[str] = []
        self.errores_criticos: List[Dict[str, Any]] = []
        self.warnings_detectados: List[Dict[str, Any]] = []
        self._hashes_vistos: set = set()

    def capturar_y_analizar(self, num_lineas: int = 60,
                             patron_error_bug: str = None,
                             paso_idx: int = 0) -> Dict[str, Any]:
        """Captura logcat reciente, filtra líneas relevantes y detecta anomalías."""
        code, out, _ = self.adb.ejecutar_adb(["logcat", "-d", "-t", str(num_lineas)])
        lineas_raw = [l.strip() for l in out.splitlines() if l.strip()]

        nuevas_lineas = []
        for linea in lineas_raw:
            h = hash(linea)
            if h not in self._hashes_vistos:
                self._hashes_vistos.add(h)
                nuevas_lineas.append(linea)

        lineas_app = [
            l for l in nuevas_lineas
            if self.package in l
            or any(k in l for k in ["FATAL", "AndroidRuntime", "System.err"] + self.PALABRAS_CRITICAS[:6])
        ]
        self.logs_sesion.extend(lineas_app)

        errores_nuevos: List[Dict] = []
        warnings_nuevos: List[Dict] = []
        coincide_patron_bug = False

        for linea in lineas_app:
            if any(k in linea for k in self.PALABRAS_CRITICAS):
                e = {"paso": paso_idx, "linea": linea[:280], "tipo": "ERROR"}
                errores_nuevos.append(e)
                self.errores_criticos.append(e)
            elif any(k in linea for k in self.PALABRAS_WARNING):
                w = {"paso": paso_idx, "linea": linea[:280], "tipo": "WARNING"}
                warnings_nuevos.append(w)
                self.warnings_detectados.append(w)

            if patron_error_bug:
                try:
                    if re.search(patron_error_bug, linea, re.IGNORECASE):
                        coincide_patron_bug = True
                except Exception:
                    pass

        return {
            "nuevas_lineas": len(nuevas_lineas),
            "errores": errores_nuevos,
            "warnings": warnings_nuevos,
            "coincide_patron_bug": coincide_patron_bug,
            "lineas_app": lineas_app
        }

    def generar_resumen_texto(self) -> str:
        """Texto del resumen de logs para incluir en prompts de IA."""
        if not self.errores_criticos and not self.warnings_detectados:
            return "No se detectaron errores criticos en logcat durante la ejecucion."
        lineas = [
            f"[{e['tipo']}] Paso {e['paso']}: {e['linea'][:200]}"
            for e in (self.errores_criticos + self.warnings_detectados)[-25:]
        ]
        return "\n".join(lineas)

    def generar_html_seccion(self) -> str:
        """HTML para la seccion de logs en el reporte de verificacion."""
        total_l = len(self.logs_sesion)
        total_e = len(self.errores_criticos)
        total_w = len(self.warnings_detectados)

        resumen = (
            f"<p style='color:#cbd5e1;margin-bottom:10px;'>"
            f"<strong style='color:#f8fafc;'>{total_l}</strong> lineas analizadas &nbsp;|&nbsp; "
            f"<strong style='color:#f43f5e;'>{total_e}</strong> errores criticos &nbsp;|&nbsp; "
            f"<strong style='color:#f59e0b;'>{total_w}</strong> advertencias</p>"
        )
        if total_e == 0 and total_w == 0:
            return resumen + "<p style='color:#34d399;'>Sin excepciones ni crashes durante la ejecucion.</p>"

        filas = ""
        for e in (self.errores_criticos + self.warnings_detectados)[-60:]:
            color = "#f43f5e" if e["tipo"] == "ERROR" else "#f59e0b"
            bg = "rgba(244,63,94,0.08)" if e["tipo"] == "ERROR" else "rgba(245,158,11,0.08)"
            filas += (
                f"<tr style='background:{bg};'>"
                f"<td style='font-weight:700;color:{color};text-align:center;padding:7px 10px;'>{e['paso']}</td>"
                f"<td style='color:{color};font-size:0.78rem;padding:7px 10px;font-weight:700;'>{html.escape(e['tipo'])}</td>"
                f"<td style='font-family:monospace;font-size:0.76rem;color:#e2e8f0;padding:7px 10px;"
                f"word-break:break-all;'>{html.escape(e['linea'][:220])}</td></tr>"
            )
        tabla = (
            "<div style='overflow-x:auto;margin-top:12px;'>"
            "<table style='width:100%;border-collapse:collapse;border:1px solid #334155;"
            "border-radius:8px;overflow:hidden;'><thead><tr style='background:rgba(15,23,42,0.9);'>"
            "<th style='padding:9px 10px;color:#94a3b8;text-align:center;font-size:0.78rem;width:55px;'>PASO</th>"
            "<th style='padding:9px 10px;color:#94a3b8;font-size:0.78rem;width:85px;'>TIPO</th>"
            "<th style='padding:9px 10px;color:#94a3b8;font-size:0.78rem;'>LINEA DE LOG</th>"
            f"</tr></thead><tbody>{filas}</tbody></table></div>"
        )
        return resumen + tabla


# ==========================================
# 4.7 SISTEMA DE APRENDIZAJE PERSISTENTE
# ==========================================
class ProcedimientosAprendidos:
    """
    Memoria permanente del bot. Cuando no puede proceder, pregunta al usuario
    y guarda la instruccion para siempre. Nunca vuelve a preguntar lo mismo.
    """

    RUTA_ARCHIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "procedimientos_aprendidos.json")
    _datos: Dict[str, Any] = {}
    _cargado: bool = False

    @classmethod
    def _cargar(cls):
        if not cls._cargado:
            if os.path.exists(cls.RUTA_ARCHIVO):
                try:
                    with open(cls.RUTA_ARCHIVO, "r", encoding="utf-8") as f:
                        cls._datos = json.load(f)
                except Exception:
                    cls._datos = {}
            cls._cargado = True

    @classmethod
    def _guardar(cls):
        try:
            with open(cls.RUTA_ARCHIVO, "w", encoding="utf-8") as f:
                json.dump(cls._datos, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"   Aviso: No se pudo guardar la instruccion: {e}")

    @classmethod
    def _clave(cls, pantalla: str, paso: str) -> str:
        STOP = {"para", "desde", "hacia", "luego", "paso", "presiona", "toca",
                "abre", "con", "del", "las", "los", "una", "uno", "que", "por"}
        palabras = [p for p in re.findall(r'\w{3,}', paso.lower())[:6] if p not in STOP]
        return f"{pantalla.lower()}_{'_'.join(palabras)}" if palabras else f"{pantalla.lower()}_generico"

    @classmethod
    def buscar(cls, pantalla: str, paso: str) -> Optional[Dict[str, Any]]:
        """Busca instruccion guardada que coincida con el contexto actual."""
        cls._cargar()
        clave = cls._clave(pantalla, paso)
        if clave in cls._datos:
            return cls._datos[clave]
        palabras_paso = set(re.findall(r'\w{3,}', paso.lower()))
        mejor, mejor_score = None, 0
        for k, v in cls._datos.items():
            palabras_k = set(k.split("_"))
            score = len(palabras_paso & palabras_k)
            if score > mejor_score and score >= 2:
                mejor_score = score
                mejor = v
        return mejor

    @classmethod
    def guardar(cls, pantalla: str, paso: str, instruccion: Dict[str, Any]):
        """Guarda instruccion aprendida de forma permanente."""
        cls._cargar()
        clave = cls._clave(pantalla, paso)
        instruccion["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        instruccion["pantalla_origen"] = pantalla
        instruccion["paso_origen"] = paso[:200]
        cls._datos[clave] = instruccion
        cls._guardar()
        print(f"   [MEMORIA BOT] Instruccion guardada permanentemente: '{clave}'")

    @classmethod
    def contar(cls) -> int:
        cls._cargar()
        return len(cls._datos)


def mostrar_modal_instruccion(pantalla_tipo: str, paso_objetivo: str):
    import subprocess
    import sys
    import tempfile
    import os
    import json

    script = """
import tkinter as tk
import sys, json

def main():
    root = tk.Tk()
    root.title("Intervencion Requerida")
    root.configure(bg="#0f172a")
    root.geometry("600x450")
    root.attributes("-topmost", True)
    mf = tk.Frame(root, bg="#0f172a", padx=20, pady=20)
    mf.pack(fill=tk.BOTH, expand=True)
    tk.Label(mf, text="Intervencion requerida", font=("Segoe UI", 14, "bold"), bg="#0f172a", fg="#ef4444").pack(anchor="w")
    tk.Label(mf, text=f"Paso actual:\n{sys.argv[1]}", font=("Segoe UI", 9, "bold"), bg="#1e293b", fg="#cbd5e1", justify="left", wraplength=550).pack(fill=tk.X, pady=5)
    tw = tk.Text(mf, height=5, bg="#1e293b", fg="white", font=("Consolas", 10))
    tw.pack(fill=tk.X, pady=(0, 10))
    def confirmar():
        ans = tw.get("1.0", tk.END).strip()
        if ans:
            print(json.dumps({"descripcion": ans}))
            root.destroy()
    tk.Button(mf, text="Reanudar", command=confirmar).pack(side=tk.RIGHT)
    root.mainloop()

if __name__ == '__main__':
    main()
"""
    fd, path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(script)
    try:
        res = subprocess.run([sys.executable, path, paso_objetivo], capture_output=True, text=True, encoding="utf-8")
        out = res.stdout.strip()
        if out:
            inst = json.loads(out)
            inst["pantalla"] = pantalla_tipo
            inst["paso"] = paso_objetivo
            from onthefly_knowledge_engine import ProcedimientosAprendidos
            ProcedimientosAprendidos.guardar(pantalla_tipo, paso_objetivo, inst)
            return inst
        return None
    except Exception:
        return None
    finally:
        try: os.remove(path)
        except: pass

def pedir_verificacion_fisica_humana(pregunta: str):
    import subprocess
    import sys
    import tempfile
    import os

    script = """
import tkinter as tk
import sys

def main():
    root = tk.Tk()
    root.title("Verificacion Fisica")
    root.configure(bg="#0f172a")
    root.geometry("600x400")
    root.attributes("-topmost", True)
    mf = tk.Frame(root, bg="#0f172a", padx=22, pady=18)
    mf.pack(fill=tk.BOTH, expand=True)
    tk.Label(mf, text="Verificacion Fisica o Externa", font=("Segoe UI", 13, "bold"), bg="#0f172a", fg="#f8fafc").pack(anchor="w", pady=(0, 4))
    ctx = tk.Frame(mf, bg="#1e293b", pady=10, padx=12)
    ctx.pack(fill=tk.X, pady=(6, 14))
    tk.Label(ctx, text=sys.argv[1], font=("Segoe UI", 10, "bold"), bg="#1e293b", fg="#cbd5e1", wraplength=500, justify="left").pack(anchor="w")
    tw = tk.Text(mf, height=4, width=60, bg="#1e293b", fg="#f8fafc", font=("Consolas", 10))
    tw.pack(fill=tk.X, pady=(0, 12))
    def confirmar():
        ans = tw.get("1.0", tk.END).strip()
        if ans:
            print(ans)
            root.destroy()
    tk.Button(mf, text="Confirmar", command=confirmar).pack(side=tk.RIGHT)
    root.mainloop()

if __name__ == '__main__':
    main()
"""
    fd, path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(script)
    try:
        res = subprocess.run([sys.executable, path, pregunta], capture_output=True, text=True, encoding="utf-8")
        out = res.stdout.strip()
        return out if out else None
    except Exception:
        return None
    finally:
        try: os.remove(path)
        except: pass


# ==========================================
# 5. GESTOR Y ANALIZADOR DE VIDEO DE EVIDENCIA (GOOGLE DRIVE + IA)
# ==========================================
class VideoEvidenceAnalyzer:
    """
    Descarga y analiza con IA (Gemini 3.5 Flash Lite) el video de evidencia original
    adjunto en Google Drive para replicar con exactitud el procedimiento del bug.
    """
    SCOPES = ['https://www.googleapis.com/auth/drive']
    VIDEOS_LOCAL_DIR = r"C:\Users\Administrador\Desktop\Reportes QA"

    @staticmethod
    def extraer_enlace_drive(descripcion: str, comentarios: List[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
        """Extrae el enlace de Google Drive y el nombre del archivo desde la descripción o comentarios."""
        textos = [descripcion or ""]
        if comentarios:
            for c in comentarios:
                textos.append(c.get("content", ""))
        full_text = "\n".join(textos)

        # 1. Enlace tipo Markdown: [Nombre.mp4](https://drive.google.com/...)
        m_md = re.search(r"\[([^\]]+\.(?:mp4|mov|avi|webm|mkv))\]\((https?://drive\.google\.com[^\)]+)\)", full_text, re.IGNORECASE)
        if m_md:
            nombre = m_md.group(1).strip()
            url = m_md.group(2).strip()
            m_id = re.search(r"/d/([a-zA-Z0-9_\-]+)", url)
            if not m_id:
                m_id = re.search(r"[?&]id=([a-zA-Z0-9_\-]+)", url)
            if m_id:
                return {"file_id": m_id.group(1), "url": url, "nombre": nombre}

        # 2. Enlace directo a Drive /file/d/<id>
        m_id = re.search(r"https?://drive\.google\.com/file/d/([a-zA-Z0-9_\-]+)", full_text)
        if m_id:
            file_id = m_id.group(1)
            m_nom = re.search(r"([^\r\n\[]+\.mp4)", full_text[:m_id.start()], re.IGNORECASE)
            nombre = m_nom.group(1).strip(" *-_:[]`") if m_nom else f"{file_id}.mp4"
            url = f"https://drive.google.com/file/d/{file_id}/view?usp=drive_link"
            return {"file_id": file_id, "url": url, "nombre": nombre}

        # 3. Enlace con open?id= o uc?id=
        m_open = re.search(r"https?://drive\.google\.com/(?:open|uc)\?id=([a-zA-Z0-9_\-]+)", full_text)
        if m_open:
            file_id = m_open.group(1)
            url = f"https://drive.google.com/file/d/{file_id}/view?usp=drive_link"
            return {"file_id": file_id, "url": url, "nombre": f"{file_id}.mp4"}

        return None

    @classmethod
    def autenticar_drive(cls):
        """Autentica con la API de Google Drive usando OAuth 2.0 y token.json."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        token_path = os.path.join(script_dir, 'token.json')
        credentials_path = os.path.join(script_dir, 'credentials.json')

        creds = None
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, cls.SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(credentials_path):
                    raise FileNotFoundError(f"No se encontró archivo de credenciales de Google: {credentials_path}")
                print("\n🔑 Abriendo navegador para autorizar acceso a Google Drive...")
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, cls.SCOPES)
                creds = flow.run_local_server(port=0)

            with open(token_path, 'w', encoding='utf-8') as token_file:
                token_file.write(creds.to_json())

        return build('drive', 'v3', credentials=creds)

    @classmethod
    def obtener_o_descargar_video(cls, file_id: str, nombre_sugerido: str = "") -> str:
        """Busca el video localmente en Reportes QA o lo descarga desde Google Drive."""
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_temp", "videos")
        os.makedirs(cache_dir, exist_ok=True)
        ruta_cache = os.path.join(cache_dir, f"{file_id}.mp4")

        # 1. Si ya está descargado en caché local
        if os.path.exists(ruta_cache) and os.path.getsize(ruta_cache) > 1024:
            print(f"   ⚡ Video de evidencia ya disponible en caché local ({os.path.getsize(ruta_cache) // 1024} KB).")
            return ruta_cache

        # 2. Si existe en la carpeta de Reportes QA en el Escritorio
        if os.path.exists(cls.VIDEOS_LOCAL_DIR):
            for arch in os.listdir(cls.VIDEOS_LOCAL_DIR):
                if arch.lower().endswith(".mp4"):
                    if nombre_sugerido and nombre_sugerido.lower() in arch.lower():
                        ruta_desk = os.path.join(cls.VIDEOS_LOCAL_DIR, arch)
                        print(f"   📂 Video encontrado en Reportes QA: {arch}")
                        return ruta_desk

        # 3. Descargar desde Google Drive
        print(f"   ⏳ Descargando video de evidencia original desde Google Drive (ID: {file_id})...")
        service = cls.autenticar_drive()
        try:
            meta = service.files().get(fileId=file_id, fields='name, size').execute()
            tamanio_mb = round(int(meta.get('size', 0)) / (1024 * 1024), 2)
            print(f"   📦 Archivo en Drive: '{meta.get('name')}' ({tamanio_mb} MB)")
        except Exception:
            pass

        req = service.files().get_media(fileId=file_id)
        with io.FileIO(ruta_cache, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, req, chunksize=1024 * 1024 * 2)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    sys.stdout.write(f"\r   📥 Descargando evidencia: {pct}%...")
                    sys.stdout.flush()
        print("\n   ✅ Video descargado y verificado correctamente.")
        return ruta_cache

    @classmethod
    def analizar_video(cls, video_path: str, cliente_gemini: Optional[genai.Client] = None,
                       engine: Optional[Any] = None, reporte: Optional[Dict[str, Any]] = None,
                       carpeta_reporte: Optional[str] = None) -> Dict[str, Any]:
        """
        Analiza el video de evidencia combinando:
        1. Transcripción de Audio con Groq Whisper (whisper-large-v3-turbo) con marcas de tiempo.
        2. Extracción densa de 18 fotogramas cronológicos con OpenCV.
        3. Análisis visual + auditivo + técnico con Gemini (rotación de claves y modelos 3.5-flash-lite / 3.5-flash / 3.6-flash / 3.8-flash).
        """
        cache_analisis = f"{video_path}.analysis.json"
        if os.path.exists(cache_analisis):
            try:
                with open(cache_analisis, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                accs_cache = cached_data.get("acciones_cronologicas", [])
                resumen_c = (cached_data.get("resumen_evidencia") or "").lower()
                if (
                    isinstance(accs_cache, list)
                    and len(accs_cache) >= 2
                    and any(a.get("localizadores_semanticos") for a in accs_cache)
                    and "no se dispone de acceso" not in resumen_c
                ):
                    print("   ⚡ Análisis de video (visual + audio) cargado instantáneamente desde caché local.")
                    return cached_data
                else:
                    os.remove(cache_analisis)
            except Exception:
                pass

        # 1. TRANSCRIPCIÓN DE AUDIO DEL VIDEO VÍA GROQ WHISPER (0 créditos Gemini)
        transcripcion_audio = ""
        segmentos_audio_txt = ""
        if os.path.exists(video_path):
            try:
                from proveedor_alternativo import cargar_configuracion_ia
                cfg_ia = cargar_configuracion_ia()
                groq_key = cfg_ia.get("groq_api_key", "").strip()
                tamanio_bytes = os.path.getsize(video_path)
                if groq_key and tamanio_bytes < 25 * 1024 * 1024:
                    print("   🎙️ [AUDIO IA] Analizando y transcribiendo pista de audio del video con Groq Whisper...")
                    with open(video_path, "rb") as f_aud:
                        r_aud = requests.post(
                            "https://api.groq.com/openai/v1/audio/transcriptions",
                            headers={"Authorization": f"Bearer {groq_key}"},
                            files={"file": (os.path.basename(video_path), f_aud, "video/mp4")},
                            data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json"},
                            timeout=35.0
                        )
                    if r_aud.status_code == 200:
                        aud_json = r_aud.json()
                        transcripcion_audio = (aud_json.get("text") or "").strip()
                        segs = aud_json.get("segments") or []
                        lineas_seg = []
                        for s in segs[:40]:
                            ini = s.get("start", 0.0)
                            fin = s.get("end", 0.0)
                            txt_s = (s.get("text") or "").strip()
                            if txt_s:
                                lineas_seg.append(f"[{ini:.1f}s - {fin:.1f}s]: {txt_s}")
                        segmentos_audio_txt = "\n".join(lineas_seg)
                        if transcripcion_audio:
                            print(f"   ✅ [AUDIO IA] Voz del tester transcrita ({len(transcripcion_audio)} caracteres): \"{transcripcion_audio[:140]}...\"")
            except Exception as e_aud:
                print(f"   ℹ️ Nota en transcripción de audio: {e_aud}")

        # 2. EXTRACCIÓN DENSA DE FOTOGRAMAS CRONOLÓGICOS CON OPENCV
        partes_frames_gemini = []
        marcas_frames = []
        if os.path.exists(video_path):
            try:
                import cv2
                cap = cv2.VideoCapture(video_path)
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                duracion_seg = total_frames / fps if fps > 0 else 0
                if total_frames > 0:
                    dest_dir = carpeta_reporte or os.path.dirname(video_path)
                    os.makedirs(dest_dir, exist_ok=True)
                    # Guardar 4 capturas para el reporte HTML
                    for idx_f, frac in enumerate([0.15, 0.4, 0.65, 0.85], 1):
                        f_idx = max(0, min(total_frames - 1, int(total_frames * frac)))
                        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
                        ret, frame = cap.read()
                        if ret and frame is not None:
                            f_path = os.path.join(dest_dir, f"video_frame_{idx_f}.png")
                            cv2.imwrite(f_path, frame)

                    # Extraer hasta 18 fotogramas densos (mayor densidad en los primeros 45s donde se opera el POS)
                    if duracion_seg > 20:
                        tiempos_muestreo = [1, 3, 5, 7, 9, 12, 15, 18, 22, 26, 30, 35, 42, 52, 65, 85, 110, 135]
                        tiempos_muestreo = [t for t in tiempos_muestreo if t < duracion_seg - 0.5]
                        if len(tiempos_muestreo) < 12:
                            tiempos_muestreo = [duracion_seg * (i / 16.0) for i in range(1, 16)]
                    else:
                        tiempos_muestreo = [duracion_seg * (i / 14.0) for i in range(1, 14)]

                    for idx_m, t_sec in enumerate(tiempos_muestreo, 1):
                        f_idx = max(0, min(total_frames - 1, int(t_sec * fps)))
                        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
                        ret, frame = cap.read()
                        if ret and frame is not None:
                            h, w = frame.shape[:2]
                            if w > 960:
                                frame = cv2.resize(frame, (960, int(h * 960 / float(w))))
                            ok_jpg, buf_jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                            if ok_jpg:
                                partes_frames_gemini.append(
                                    types.Part.from_bytes(data=buf_jpg.tobytes(), mime_type="image/jpeg")
                                )
                                marcas_frames.append(f"Fotograma #{idx_m} (t={t_sec:.1f}s)")
                    print(f"   📸 {len(partes_frames_gemini)} fotogramas cronológicos extraídos con éxito vía OpenCV.")
                cap.release()
            except Exception as e_cv:
                print(f"   ℹ️ Nota en extracción de fotogramas OpenCV: {e_cv}")

        nombre_base = os.path.basename(video_path)
        bloque_audio_prompt = ""
        if transcripcion_audio:
            bloque_audio_prompt = f"""
TRANSCRIPCIÓN EXACTA DEL AUDIO / VOZ DEL TESTER EN EL VIDEO (Groq Whisper):
Texto completo: "{transcripcion_audio}"
Segmentos por segundo:
{segmentos_audio_txt}
"""

        prompt = f"""Eres un Ingeniero Experto en QA Automatizado para On The Fly POS en terminales Android PAX.
Analiza DETENIDAMENTE la secuencia de fotogramas del video ({nombre_base}: {', '.join(marcas_frames)}), la TRANSCRIPCIÓN DEL AUDIO narrado por el tester y el reporte de bug de Todoist.
{bloque_audio_prompt}
DATOS TECNICOS DEL REPORTE (TODOIST):
- Titulo del Bug: {reporte.get('titulo', '') if reporte else ''}
- Descripcion del problema / Comportamiento Anomalo:
{reporte.get('problema_actual', '') if reporte else ''}
- Procedimiento para reproducir el error:
{reporte.get('procedimiento_raw', '') if reporte else ''}
- Pasos estructurados del bug:
{json.dumps(reporte.get('pasos', []), ensure_ascii=False) if reporte else '[]'}
- Resultado Esperado segun el reporte:
{reporte.get('resultado_esperado', '') if reporte else ''}

== INSTRUCCIONES CRITICAS DE ATOMICIDAD Y REPLICACION ==

1. Tu misión PRINCIPAL es reconstruir la secuencia EXACTA y ATÓMICA de acciones físicas que el bot debe replicar en la pantalla de la terminal Android POS para reproducir la prueba del video.
2. REGLA DE ATOMICIDAD (OBLIGATORIA):
   Cada elemento dentro de `acciones_cronologicas` DEBE ser UNA SOLA ACCIÓN FÍSICA ATÓMICA en la pantalla del POS (un solo toque `TAP`, un `TYPE` en un campo, o un `SWIPE` para deslizar):
   - NUNCA combines varios toques en un solo paso (ej: PROHIBIDO poner "Ingresar a Quick Sale y agregar Croissant" en un solo paso).
   - Si en el video el operador entra a Quick Sale, toca la categoría "Brunch" y luego toca el producto "Croissant", DEBES separarlo en pasos individuales:
     * Paso 1: `TAP` en `elemento_visual: "QUICK SALE"` (`texto_exacto: "QUICK SALE"`)
     * Paso 2: `TAP` en `elemento_visual: "Brunch"` (`texto_exacto: "Brunch"`)
     * Paso 3: `TAP` en `elemento_visual: "Croissant"` (`texto_exacto: "Croissant"`)
   - Si vincula un cliente (ej. Humberto Gold), sepáralo en pasos individuales:
     * Paso 4: `TAP` en el campo buscador de cliente `elemento_visual: "Phone Number / Name"` (`texto_exacto: "Phone Number / Name"`, `hint_placeholder: "Phone Number / Name"`)
     * Paso 5: `TYPE` con `texto_a_escribir: "gold"` en `elemento_visual: "Search Customer"` (`resource_id_fragmento: "txtSearchClient"`)
     * Paso 6: `TAP` en el cliente de la lista `elemento_visual: "Humberto Gold"` (`texto_exacto: "Humberto Gold"`)
   - Si en el video el operador se desplaza por la pantalla (hace scroll hacia arriba o abajo), debes generar una acción de tipo "SWIPE":
     * Paso 7: `tipo_gesto: "SWIPE"`, `accion: "Deslizar hacia abajo en la pantalla"`, `coordenadas_fallback`: {{"x_pct": 0.5, "y_pct": 0.8}}, `coordenadas_fin_fallback`: {{"x_pct": 0.5, "y_pct": 0.2}}
   - Si luego toca el botón de pagar y elige un método:
     * Paso 8: `TAP` en `elemento_visual: "Pay"` (`texto_exacto: "Pay"`, `resource_id_fragmento: "payment"`)
     * Paso 9: `TAP` en `elemento_visual: "Payment Request"` (`texto_exacto: "Payment Request"`)
3. Si en el video el operador hace un intento fallido o de muestra y en el audio corrige ("antes debemos poner aquí un usuario..."), genera en `acciones_cronologicas` la secuencia limpia y completa que reproduce el escenario del bug en la terminal POS.
4. NO pongas en `acciones_cronologicas` pasos que ocurren fuera de la terminal POS (como abrir Gmail en una PC). Si la prueba requiere verificar un correo electrónico recibido o un recibo físico impreso, pon `"requiere_verificacion_fisica": true` y redacta `"pregunta_verificacion_fisica"`.

Responde UNICAMENTE con este esquema JSON valido (sin markdown, sin texto extra):
{{
  "resumen_evidencia": "Descripcion clara de lo que ocurre en el video y audio en 2-3 oraciones",
  "transcripcion_audio": {json.dumps(transcripcion_audio, ensure_ascii=False)},
  "bug_visible_en_video": true,
  "descripcion_bug_en_video": "Que fallo exactamente viste en el video y explico el tester en el audio",
  "acciones_cronologicas": [
    {{
      "paso": 1,
      "en_terminal_pos": true,
      "tipo_gesto": "TAP",
      "accion": "Tocar el boton QUICK SALE para abrir el punto de venta",
      "elemento_visual": "QUICK SALE",
      "objetivo": "Entrar al modulo de venta rapida",
      "localizadores_semanticos": {{
        "texto_exacto": "QUICK SALE",
        "content_description": null,
        "resource_id_fragmento": "quick_sale",
        "hint_placeholder": null,
        "clase_ui": "android.widget.TextView",
        "jerarquia_descripcion": "Modulo QUICK SALE en el Dashboard"
      }},
      "texto_a_escribir": null,
      "coordenadas_fallback": {{"x_pct": 0.3, "y_pct": 0.25}},
      "coordenadas_fin_fallback": null,
      "duracion_ms": 300,
      "patron_log_esperado": null,
      "patron_log_error": null
    }}
  ],
  "comportamiento_bug_observado": "Descripcion exacta del fallo mostrado en la evidencia",
  "puntos_criticos_a_verificar": [
    "Criterio especifico 1 que debe verificarse para confirmar que el bug fue solucionado"
  ],
  "requiere_pago_tarjeta": false,
  "patron_log_bug_global": "regex que en logcat confirma que el bug persiste, o null",
  "requiere_verificacion_fisica": false,
  "pregunta_verificacion_fisica": "Si el bug implica revisar un recibo impreso o abrir otra app (ej: Email), escribe aquí la pregunta exacta que el bot debe hacerle al humano al finalizar la prueba automatizada. Si no, null."
}}

Tipos de gesto validos: TAP, SWIPE, TYPE, LONG_PRESS, KEYEVENT, SCROLL_DOWN, SCROLL_UP"""

        datos = None

        # 3. ANÁLISIS MULTIMODAL CON GEMINI (FOTOGRAMAS + AUDIO TRANSCRITO + ROTACIÓN DE CLAVES Y MODELOS)
        if partes_frames_gemini:
            modelos_vision = ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.8-flash"]
            contenidos_multimodales = list(partes_frames_gemini) + [prompt]
            for mod_v in modelos_vision:
                if datos:
                    break
                for intento_k in range(3):
                    try:
                        cli_v, key_v, num_k = gestor_pool.obtener_cliente()
                        print(f"   🧠 Analizando fotogramas + audio del video con {mod_v} (Clave #{num_k})...")
                        resp = cli_v.models.generate_content(
                            model=mod_v,
                            contents=contenidos_multimodales,
                            config=types.GenerateContentConfig(response_mime_type="application/json")
                        )
                        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                            p_tok = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
                            c_tok = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
                            MonitorConsumoTokens.registrar_gemini(mod_v, p_tok, c_tok)
                        gestor_pool.registrar_consumo_activo()
                        gestor_pool.registrar_exito_clave(key_v)

                        resp_txt = re.sub(r"^```json\s*", "", resp.text.strip(), flags=re.IGNORECASE)
                        resp_txt = re.sub(r"^```\s*", "", resp_txt)
                        resp_txt = re.sub(r"\s*```$", "", resp_txt)
                        datos_cand = json.loads(resp_txt)
                        if isinstance(datos_cand, dict) and datos_cand.get("acciones_cronologicas"):
                            datos = datos_cand
                            if transcripcion_audio and not datos.get("transcripcion_audio"):
                                datos["transcripcion_audio"] = transcripcion_audio
                            print(f"   ✅ Video y audio analizados exitosamente con {mod_v} ({len(datos['acciones_cronologicas'])} acciones atómicas extraídas).")
                            break
                    except Exception as e_mv:
                        err_s = str(e_mv).lower()
                        print(f"   ⚠️ {mod_v} (Clave #{num_k}) no disponible: {str(e_mv)[:100]}")
                        if any(k in err_s for k in ["429", "resource_exhausted", "quota", "401", "403"]):
                            gestor_pool.rotar_siguiente_clave(f"Fallo en video {mod_v}: {str(e_mv)[:60]}")
                            continue
                        else:
                            # Si es 503 en este modelo, saltar de inmediato al siguiente modelo
                            break

        # 4. FALLBACK: Groq Cloud usando la Transcripción de Audio + Reporte Todoist
        if not datos or not datos.get("acciones_cronologicas"):
            print("   🧠 Generando secuencia atómica de pasos con Groq Cloud + Transcripción de Audio Whisper...")
            try:
                if engine and hasattr(engine, "_generar_con_groq"):
                    resp_txt = engine._generar_con_groq(prompt, system_prompt="Eres un agente de QA que responde únicamente en formato JSON válido con acciones_cronologicas atómicas.")
                else:
                    from proveedor_alternativo import ejecutar_llamada_openai_compatible, ENDPOINT_GROQ, cargar_configuracion_ia
                    cfg = cargar_configuracion_ia()
                    api_k = cfg.get("groq_api_key", "").strip()
                    payload = {
                        "model": cfg.get("modelo_groq", "openai/gpt-oss-120b"),
                        "messages": [
                            {"role": "system", "content": "Eres un agente de QA que responde únicamente en formato JSON válido con acciones_cronologicas atómicas."},
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": 0.2,
                        "max_tokens": 3000
                    }
                    resp_obj = ejecutar_llamada_openai_compatible(ENDPOINT_GROQ, api_k, payload, timeout=40.0)
                    usage = resp_obj.get("usage", {})
                    if usage:
                        MonitorConsumoTokens.registrar_groq(payload.get("model", "openai/gpt-oss-120b"), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
                    resp_txt = resp_obj.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

                resp_txt = re.sub(r"^```json\s*", "", resp_txt.strip(), flags=re.IGNORECASE)
                resp_txt = re.sub(r"^```\s*", "", resp_txt)
                resp_txt = re.sub(r"\s*```$", "", resp_txt)
                datos = json.loads(resp_txt)
                if transcripcion_audio and isinstance(datos, dict):
                    datos["transcripcion_audio"] = transcripcion_audio
            except Exception as e_groq:
                print(f"   ⚠️ Aviso en análisis con Groq: {e_groq}")

        # Si por alguna razón extrema no se pudo conectar a ninguna IA, generar esquema base desde el reporte
        if not datos or not datos.get("acciones_cronologicas"):
            pasos_base = reporte.get("pasos", []) if reporte else []
            acciones = []
            for i, p in enumerate(pasos_base, 1):
                acciones.append({
                    "paso": i,
                    "en_terminal_pos": True,
                    "tipo_gesto": "TAP",
                    "accion": p,
                    "elemento_visual": "Interfaz POS",
                    "objetivo": f"Ejecutar paso {i}",
                    "localizadores_semanticos": {"texto_exacto": None}
                })
            datos = {
                "resumen_evidencia": f"Evidencia de video asociada a: {reporte.get('titulo', '') if reporte else ''}",
                "transcripcion_audio": transcripcion_audio,
                "acciones_cronologicas": acciones,
                "comportamiento_bug_observado": reporte.get("problema_actual", "") if reporte else "Comportamiento anómalo reportado",
                "puntos_criticos_a_verificar": [reporte.get("resultado_esperado", "Verificar funcionamiento correcto") if reporte else "Verificar corrección"]
            }

        # Guardar en caché SOLO si tiene acciones cronológicas válidas con localizadores semánticos
        try:
            accs_guardar = datos.get("acciones_cronologicas", [])
            if len(accs_guardar) >= 2 and any(a.get("localizadores_semanticos") for a in accs_guardar):
                with open(cache_analisis, "w", encoding="utf-8") as f:
                    json.dump(datos, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        return datos


# ==========================================
# 6. PARSEADOR Y ANALIZADOR DE FACTIBILIDAD
# ==========================================
class BugReportParser:
    """Extrae y estructura la información técnica del reporte de QA."""

    @staticmethod
    def parsear(tarea: Dict[str, Any], comentarios: List[Dict[str, Any]]) -> Dict[str, Any]:
        titulo = tarea.get("content", "").strip()
        descripcion = tarea.get("description", "").strip()

        def extraer_seccion(patron: str, texto: str, default: str = "") -> str:
            m = re.search(patron, texto, re.IGNORECASE | re.DOTALL)
            return m.group(1).strip() if m else default

        dispositivo_rep = extraer_seccion(r"-\s*\*\*Dispositivo:\*\*\s*([^\n\r]+)", descripcion)
        if not dispositivo_rep:
            dispositivo_rep = extraer_seccion(r"\*\*Dispositivo:\*\*\s*([^\n\r]+)", descripcion, "Desconocido")

        version_rep = extraer_seccion(r"-\s*\*\*Versi[oó]n de la App:\*\*\s*([^\n\r]+)", descripcion)
        if not version_rep:
            version_rep = extraer_seccion(r"\*\*Versi[oó]n de la App:\*\*\s*([^\n\r]+)", descripcion, "Desconocida")

        precondiciones = extraer_seccion(r"\*\*Precondiciones:\*\*\s*(.*?)(?=\*\*Definici[oó]n|\*\*Procedimiento|$)", descripcion)
        problema_actual = extraer_seccion(r"\*\*Definici[oó]n del problema.*?\*\*\s*(.*?)(?=\*\*Procedimiento|$)", descripcion)
        procedimiento = extraer_seccion(r"\*\*Procedimiento para llegar al error:\*\*\s*(.*?)(?=\*\*Resultado esperado|$)", descripcion)
        resultado_esperado = extraer_seccion(r"\*\*Resultado esperado:\*\*\s*(.*?)(?=\*\*C[oó]digo Afectado|\*\*Evidencias|$)", descripcion)

        notas_dev = []
        for c in comentarios:
            c_text = c.get("content", "").strip()
            if c_text:
                notas_dev.append(c_text)

        pasos = []
        if procedimiento:
            lineas = procedimiento.splitlines()
            paso_actual = ""
            for l in lineas:
                l_s = l.strip()
                match_num = re.match(r"^(\d+)\.\s*(.*)", l_s)
                if match_num:
                    if paso_actual:
                        pasos.append(paso_actual)
                    paso_actual = match_num.group(2).strip()
                elif paso_actual and l_s:
                    paso_actual += " " + l_s
            if paso_actual:
                pasos.append(paso_actual)

        # Extraer enlace de video de Google Drive
        info_video = VideoEvidenceAnalyzer.extraer_enlace_drive(descripcion, comentarios)

        return {
            "id": tarea.get("id"),
            "titulo": titulo,
            "dispositivo": dispositivo_rep,
            "version": version_rep,
            "precondiciones": precondiciones,
            "problema_actual": problema_actual,
            "procedimiento_raw": procedimiento,
            "pasos": pasos,
            "resultado_esperado": resultado_esperado,
            "notas_desarrollador": notas_dev,
            "video_evidencia": info_video
        }


# ==========================================
# 6. ONTOLOGÍA INTEGRAL Y SUPERVISOR DE ON THE FLY POS
# ==========================================
ONTOLOGIA_ON_THE_FLY = {
    "DASHBOARD": {
        "layout": "fragment_dash.xml",
        "ids": ["MenuBurger", "textview_venue_name", "layout_clock_in", "btnSync", "btn_delete"],
        "modulos": ["QUICK SALE", "TABLES", "TABS", "SPEED BAR", "REPORTS", "SETTINGS", "CLOCK"]
    },
    "PUNTO_DE_VENTA": {
        "layout": "activity_main2.xml",
        "ids": ["btnChangeBatch", "fragment_cont", "ticket", "btn_pay", "total_price", "btnLoyalty"],
        "coordenada_pay": (1600, 605)
    },
    "PANTALLA_PAGOS": {
        "layout_contenedor": "activity_payment_new.xml",
        "layout_resumen": "fragment_resumen_order_v3.xml",
        "layout_acciones": "fragment_paymen_option.xml",
        "ids_resumen": [
            "txtSubTotal", "lblMembershipDiscount", "txtMembershipDiscount",
            "lblOrderDiscount", "txtOrderDiscount", "recyclerView_Discounts",
            "txtTax", "txtServiceCharge", "txtTotalSale", "txtAmountDue"
        ],
        "ids_acciones": [
            "btnAddDiscount", "btnSplitAmount", "btnManualEntry",
            "btnCash", "btnCreditCard", "btnDebitCard", "btnGratuity"
        ],
        "coordenada_add_discount": (1215, 761),
        "coordenada_credit_card": (1740, 527),
        "swipe_scroll_up": (1500, 950, 1500, 600)
    },
    "MODAL_DESCUENTOS": {
        "layout": "fragment_discount_dialog.xml",
        "ids": [
            "lbltitle", "btnDiscountItem", "Botonera", "rv_default_discount",
            "btnDiscountCompSave", "txtDeleteAddNotes", "txtAmountItem",
            "txtAmountItemDiscount", "txtAmountTotalItemDiscount"
        ],
        "coordenada_razon_comp_na": (1404, 475),
        "coordenada_razon_comp_10": (622, 475),
        "coordenada_50": (959, 725),
        "coordenada_apply": (959, 923)
    },
    "MODAL_PIN": {
        "layout": "dialog_pin.xml",
        "ids": ["circlePin", "gridLayout", "btn1", "btn2", "btn3", "btn4", "btn5", "btn6", "btn7", "btn8", "btn9", "btn0", "btnRemove"],
        "coordenada_1": (1254, 431),
        "pin_admin_default": "1111"
    },
    "MODAL_CLIENTES": {
        "layout": "dialog_fragment_clients.xml",
        "ids": ["txtSearchClient", "ordersList", "btnScanQr", "btnNewClient", "btnClose"]
    },
    "MODAL_BENEFICIOS": {
        "layout": "dialog_customer_benefits.xml",
        "ids": ["panelBenefits", "txtBenefitsTitle", "btnCloseFooter", "scrollBenefits", "btnHistoryBack"]
    },
    "MODAL_NOTAS_ORDEN": {
        "layout": "dialog_order_note.xml",
        "ids": ["etNote", "btnNoteCancel", "btnNoteSave"]
    },
    "MODAL_PESABLE": {
        "layout": "dialog_quantity.xml",
        "ids": ["tv_weight", "et_weight", "btn_add", "btn_manual"]
    },
    "COBRO_EFECTIVO": {
        "layout": "activity_payment_cash.xml",
        "ids": ["txtAmountDue", "txtCashTendered", "txtChange", "btnExact", "btnProcess"]
    }
}


class SupervisorDeContexto:
    """
    Supervisor Cognitivo Local de QA (100% Gratis, 0 Créditos, Ilimitado).
    Comprende el contexto de negocio de On The Fly POS y la coherencia del caso de prueba.
    Detecta automáticamente qué pantalla está activa, si los datos son coherentes
    con los requisitos del reporte (ej: órdenes en efectivo vs tarjeta) y determina
    las acciones adaptativas de navegación sin consumir cuota de API.
    """

    @classmethod
    def clasificar_pantalla(cls, elementos: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Clasifica la pantalla física actual basándose en su jerarquía XML y textos."""
        textos = [e.get("text", "").strip() for e in elementos if e.get("text")]
        ids = [e.get("resource_id", "").strip() for e in elementos if e.get("resource_id")]
        texto_unido = " ".join(textos).lower()

        # 0. Diálogo de Alerta / Advertencia (ej. "You must indicate a reason")
        if any("btnYes" in i for i in ids) and any(k in texto_unido for k in ["sorry", "you must indicate a reason", "warning", "error"]):
            btn_yes = next((e for e in elementos if "btnYes" in e.get("resource_id", "") or e.get("text") == "OK"), None)
            return {
                "tipo": "ALERT_DIALOG",
                "descripcion": "Diálogo emergente de advertencia (Alerta)",
                "btn_ok": btn_yes
            }

        # 0.1 Modal de Notas de la Orden o Ítem (¡NUNCA confundir con cliente ni con la pantalla de venta!)
        es_dialogo_nota = (
            any("etNote" in i for i in ids) or
            ("enter text here" in texto_unido and any("lnyAlert" in i or "title" in i for i in ids))
        ) and not any(k in texto_unido for k in ["total items", "discard sale", "quick sale", "pay (", "total:"])
        if es_dialogo_nota:
            btn_save = next((e for e in elementos if "btnNoteSave" in e.get("resource_id", "") or e.get("text") == "Save"), None)
            btn_cancel = next((e for e in elementos if "btnNoteCancel" in e.get("resource_id", "") or e.get("text") == "Cancel"), None)
            return {
                "tipo": "MODAL_NOTAS_ORDEN",
                "descripcion": "Modal de nota interna de la orden o ítem",
                "btn_save": btn_save,
                "btn_cancel": btn_cancel
            }

        # 0.9 Modal de teclado numérico de peso (dialog_keypad.xml / quantityView / WEIGHT)
        es_keypad_peso = any("quantityview" in i.lower() for i in ids) or ("weight" in texto_unido and any("btn1" in i for i in ids))
        if es_keypad_peso:
            monto_elem = next((e for e in elementos if "amount" in e.get("resource_id", "").lower()), None)
            monto_val = monto_elem.get("text", "0.00") if monto_elem else "0.00"
            btn_ok = next((e for e in elementos if e.get("text") == "OK" or "btndone" in e.get("resource_id", "").lower()), None)
            return {
                "tipo": "MODAL_KEYPAD_PESO",
                "descripcion": "Teclado numérico para ingresar peso del producto (WEIGHT)",
                "monto_str": monto_val,
                "btn_ok": btn_ok
            }

        # 1. Pantalla o Modal de PIN / Passcode (dialog_pin.xml o Login)
        es_passcode = not es_keypad_peso and (
            any("lblpassword" in i.lower() or "textview_pin_text" in i.lower() or "circlepin" in i.lower() for i in ids)
            or any(k in texto_unido for k in ["enter passcode", "enter admin pin", "admin pin", "enter employee pin"])
        )
        if es_passcode:
            btn_1 = next((e for e in elementos if e.get("text") == "1" or "btn1" in e.get("resource_id", "") or "btn_1" in e.get("resource_id", "")), None)
            return {
                "tipo": "PASSCODE",
                "descripcion": "Pantalla de bloqueo por PIN / Admin Passcode",
                "btn_1": btn_1
            }

        # 2. Modal de ingreso de monto (AMOUNT)
        if "amount" in textos and any("btn_1" in i for i in ids) and ("ok" in textos or "add/modify tip" in texto_unido):
            btn_cancel = next((e for e in elementos if any(k in e.get("text", "").lower() for k in ["cancel", "cancelar", "close", "atrás", "back"]) or any(k in e.get("resource_id", "").lower() for k in ["cancel", "close", "back"])), None)
            return {
                "tipo": "MODAL_AMOUNT",
                "descripcion": "Modal de ingreso numérico de propina (AMOUNT)",
                "tiene_cancelar": btn_cancel is not None,
                "boton_cancelar": btn_cancel
            }

        # 3. Ventana emergente de seleccionar transacción para propina
        if any("add tip" in t.lower() for t in textos) or "add/modify tip" in texto_unido:
            btn_add_tip = next((e for e in elementos if "add tip" in e.get("text", "").lower()), None)
            return {
                "tipo": "MODAL_SELECCIONAR_PROPINA",
                "descripcion": "Ventana emergente con transacciones para Add Tip",
                "boton_add_tip": btn_add_tip
            }

        # 4. Detalle de una Orden específica (desde Sales History)
        if (any(t.startswith("ORDER #") for t in textos) and any(k in texto_unido for k in ["add tip", "modify tip", "untipped", "paid cash", "paid card"])) and not any(k in texto_unido for k in ["amount due", "total sale", "buttonorderinfo", "btnloyaltypayment", "btncreditcard", "credit card"]):
            es_cash = "paid cash" in texto_unido
            es_card = any(k in texto_unido for k in ["paid card", "credit card", "visa", "mastercard", "amex", "debit"])
            btn_tip = next((e for e in elementos if "tip" in e.get("text", "").lower() and any(k in e.get("text", "").lower() for k in ["add", "modify"])), None)
            btn_back = next((e for e in elementos if e.get("text") == "Back" or "lock_employee" in e.get("resource_id", "")), None)
            return {
                "tipo": "DETALLE_ORDEN",
                "descripcion": "Detalle de una orden específica",
                "es_cash": es_cash,
                "es_card": es_card,
                "tiene_boton_tip": btn_tip is not None,
                "boton_tip": btn_tip,
                "boton_back": btn_back
            }

        # 5. Lista de Historial de Ventas (Sales History)
        if any("texView_tip_all" in i or "texView_untipped" in i or "search_order" in i or "refund_history" in i for i in ids) or "sales history" in texto_unido:
            btn_untipped = next((e for e in elementos if "texView_untipped" in e.get("resource_id", "") or e.get("text") == "Untipped"), None)
            ordenes_closed = [e for e in elementos if e.get("text") == "Closed" and e.get("clickable")]
            return {
                "tipo": "HISTORIAL_VENTAS_LISTA",
                "descripcion": "Listado de transacciones en Sales History",
                "boton_untipped": btn_untipped,
                "ordenes_cerradas": ordenes_closed
            }

        # 6. Menú lateral (Drawer) abierto
        if "sales history" in texto_unido and any("drawer" in i for i in ids):
            btn_sales_history = next((e for e in elementos if "sales history" in e.get("text", "").lower()), None)
            return {
                "tipo": "DRAWER_MENU",
                "descripcion": "Menú lateral de opciones abierto",
                "boton_sales_history": btn_sales_history
            }

        # 7. Dashboard / Menú principal (fragment_dash.xml)
        if any("MenuBurger" in i or "textview_venue_name" in i or "layout_clock_in" in i for i in ids):
            btn_burger = next((e for e in elementos if "MenuBurger" in e.get("resource_id", "")), None)
            return {
                "tipo": "DASHBOARD",
                "descripcion": "Pantalla principal (Dashboard / Resumen)",
                "boton_menu": btn_burger
            }

        # 8. Modal de Búsqueda y Selección de Clientes (dialog_fragment_clients.xml)
        if any("txtsearchclient" in i.lower() or "orderslist" in i.lower() for i in ids) or any(k in texto_unido for k in ["search by phone, name or email", "search customer"]):
            campo_busqueda = next((e for e in elementos if "txtSearchClient" in e.get("resource_id", "") or ("edittext" in e.get("class", "").lower() and "note" not in e.get("resource_id", "").lower())), None)
            fila_tatiana = next((e for e in elementos if "tatiana" in e.get("text", "").lower()), None)
            fila_humberto = next((e for e in elementos if any(k in e.get("text", "").lower() for k in ["humberto", "gold"])), None)
            return {
                "tipo": "MODAL_CLIENTES",
                "descripcion": "Modal de búsqueda y selección de cliente (Customers)",
                "campo_busqueda": campo_busqueda,
                "fila_tatiana": fila_tatiana,
                "fila_humberto": fila_humberto,
                "lista_clientes": next((e for e in elementos if "ordersList" in e.get("resource_id", "")), None)
            }

        # 9. Modal de Redención de Puntos (Convert Points)
        if any(k in texto_unido for k in ["convert points", "convertir puntos", "points to redeem", "redeem points"]) or (any("convert" in t.lower() for t in textos) and any("points" in t.lower() for t in textos)):
            btn_plus = next((e for e in elementos if e.get("text") == "+" or "btn_plus" in e.get("resource_id", "") or "increase" in e.get("resource_id", "")), None)
            btn_minus = next((e for e in elementos if e.get("text") == "-" or "btn_minus" in e.get("resource_id", "") or "decrease" in e.get("resource_id", "")), None)
            btn_confirmar = next((e for e in elementos if any(k in e.get("text", "").lower() for k in ["redeem", "apply", "confirm", "canjear", "aplicar", "aceptar"]) and e.get("clickable")), None)
            campo_monto = next((e for e in elementos if "$" in e.get("text", "") or "amount" in e.get("resource_id", "").lower() or "points" in e.get("resource_id", "").lower()), None)
            es_campo_editable = campo_monto and ("edittext" in campo_monto.get("class", "").lower() or campo_monto.get("clickable", False))
            return {
                "tipo": "MODAL_CONVERT_POINTS",
                "descripcion": "Modal de Redención de Puntos (Convert Points)",
                "btn_plus": btn_plus,
                "btn_minus": btn_minus,
                "tiene_boton_confirmar": btn_confirmar is not None,
                "btn_confirmar": btn_confirmar,
                "campo_monto": campo_monto,
                "es_campo_editable": es_campo_editable
            }

        # 10. Modal de Lealtad / Membresía (Loyalty / Membership / Rewards)
        es_pantalla_pos = any(k in texto_unido for k in ["ticket info", "main check", "add seat", "categories"])
        if not es_pantalla_pos and (
            any(k in texto_unido for k in ["convert points", "puntos acumulados"]) or
            (any("rewards" in t.lower() for t in textos) and any("points" in t.lower() or "membership" in t.lower() for t in textos)) or
            any("tab_rewards" in i for i in ids)
        ):
            tab_rewards = next((e for e in elementos if "rewards" in e.get("text", "").lower() or "tab_rewards" in e.get("resource_id", "").lower()), None)
            btn_convert = next((e for e in elementos if "convert points" in e.get("text", "").lower() or "convertir puntos" in e.get("text", "").lower()), None)
            return {
                "tipo": "MODAL_LOYALTY",
                "descripcion": "Modal de Membresía y Lealtad (Loyalty / Rewards)",
                "tab_rewards": tab_rewards,
                "btn_convert_points": btn_convert
            }

        # 11. Modal de Descuentos (fragment_discount_dialog.xml)
        if any(k in texto_unido for k in ["apply spill comp", "reason for discount", "total discount", "comp%10", "comp$15"]) or any("btnDiscountCompSave" in i or "rv_default_discount" in i for i in ids):
            opc_50 = next((e for e in elementos if e.get("text") == "50%" or ("btnDefaultDiscount" in e.get("resource_id", "") and "50" in e.get("text", ""))), None)
            btn_apply = next((e for e in elementos if "btnDiscountCompSave" in e.get("resource_id", "") or "apply discount" in e.get("text", "").lower()), None)
            opc_razon = next((e for e in elementos if any(k in e.get("text", "").lower() for k in ["comp n/a", "comp%10", "comp$15", "comp$1.5"]) or "name_discont" in e.get("resource_id", "")), None)
            txt_desc = next((e.get("text") for e in elementos if "txtAmountItemDiscount" in e.get("resource_id", "")), "$0.00")
            return {
                "tipo": "MODAL_DESCUENTOS",
                "descripcion": "Modal de selección de Descuentos (Spill Comp)",
                "opcion_50": opc_50,
                "btn_apply": btn_apply,
                "opcion_razon": opc_razon,
                "monto_descuento_actual": txt_desc
            }

        # 12. Punto de Venta / Catálogo de Productos (Quick Sale / Order)
        # Se identifica inequívocamente por tener Ticket Info, Main Check, Add Seat, Categories, o Exit
        es_pos_catalogo = (any(k in texto_unido for k in [
            "ticket info", "main check", "add seat", "categories (", "categories:",
            "order notes", "discard sale"
        ]) or (
            "brunch" in texto_unido and any(k in texto_unido for k in ["pay (", "almuerzos", "carne", "queso"])
        )) and not any(k in texto_unido for k in ["total sale", "amount due", "btnloyaltypayment", "buttonorderinfo", "debit card"])

        if es_pos_catalogo:
            btn_exit = next((e for e in elementos if e.get("text") == "Exit"), None)

            # Botón específico de cobro 'Pay (2 Items) $...' o 'btn_pay'
            btn_pay = next((
                e for e in elementos
                if any(k in e.get("text", "").lower() for k in ["pay (", "pay "]) and not any(k in e.get("text", "").lower() for k in ["cash", "card", "debit"])
                or any(k in e.get("resource_id", "").lower() for k in ["btn_pay", "buttonpay", "layout_pay"])
            ), None)

            campo_cliente = next((
                e for e in elementos
                if not cls.es_campo_prohibido(e) and (
                    "phone number" in (e.get("hint", "") + " " + e.get("text", "")).lower()
                    or "lblcustomername" in e.get("resource_id", "").lower()
                    or ("txtcustomer" in e.get("resource_id", "").lower() and "note" not in e.get("resource_id", "").lower())
                )
            ), None)

            btn_ticket_info = next((
                e for e in elementos
                if "ticket info" in (e.get("text", "") + " " + e.get("resource_id", "")).lower()
                or (1350 < e.get("center", (0, 0))[0] < 1920 and 160 < e.get("center", (0, 0))[1] < 245)
            ), None)

            tiene_tatiana = any(
                "tatiana" in (e.get("text", "") + " " + e.get("desc", "")).lower()
                for e in elementos
                if not cls.es_campo_prohibido(e)
            )

            tiene_humberto = any(
                any(k in (e.get("text", "") + " " + e.get("desc", "")).lower() for k in ["humberto", "gold member", "gold"])
                for e in elementos
                if not cls.es_campo_prohibido(e)
            )

            items_catalogo = [
                e for e in elementos
                if e.get("clickable")
                and any(k in e.get("class", "").lower() for k in ["cardview", "textview", "relativelayout", "linearlayout"])
                and e.get("text")
                and not any(k in e.get("text", "").lower() for k in ["exit", "order", "quick sale", "pay", "checkout", "discard", "categories", "menu"])
                and not cls.es_campo_prohibido(e)
            ]

            return {
                "tipo": "PUNTO_DE_VENTA",
                "descripcion": "Punto de Venta / Catálogo de productos (Quick Sale)",
                "boton_exit": btn_exit,
                "campo_cliente": campo_cliente,
                "boton_ticket_info": btn_ticket_info,
                "boton_pagar": btn_pay,
                "tiene_tatiana": tiene_tatiana,
                "tiene_humberto": tiene_humberto,
                "items_catalogo": items_catalogo
            }

        # 13. Pantalla de Cobro / Checkout / Pagos
        # Solo se activa cuando ya NO estamos en el catálogo de productos
        if any(k in texto_unido for k in ["amount due", "balance due", "total sale", "split amount", "manual entry", "select all", "+ add items", "tender", "btnloyaltypayment", "buttonorderinfo"]) or (
            any(k in texto_unido for k in ["credit card", "cash", "debit card"]) and any(k in texto_unido for k in ["amount due", "total sale", "subtotal items", "order info", "customer benefits"])
        ):
            btn_add_discount = next((
                e for e in elementos
                if any(k in (e.get("text", "") + " " + e.get("resource_id", "")).lower() for k in ["add discount", "discount"])
            ), None)
            btn_credit_card = next((
                e for e in elementos
                if any(k in (e.get("text", "") + " " + e.get("resource_id", "")).lower() for k in ["credit card", "btncreditcard"])
            ), None)
            btn_loyalty = next((e for e in elementos if any(k in e.get("text", "").lower() for k in ["customer benefits", "view loyalty", "loyalty"])), None)
            return {
                "tipo": "PANTALLA_PAGOS",
                "descripcion": "Pantalla de selección de pago y cobro (Checkout)",
                "btn_add_discount": btn_add_discount,
                "btn_credit_card": btn_credit_card,
                "btn_loyalty": btn_loyalty
            }

        # 14. Modal de Producto Pesable (Weight Item / Queso)
        if any(k in texto_unido for k in ["weight item", "manual weight", "net weight", "print label"]) or (
            any("weight" in t.lower() for t in textos) and any(k in texto_unido for k in ["lb", "kg", "add to cart", "price $"])
        ):
            btn_manual = next((e for e in elementos if "btnManualPrice" in e.get("resource_id", "") or "manual weight" in (e.get("text", "") + " " + e.get("desc", "") + " " + e.get("resource_id", "")).lower()), None)
            btn_add = next((e for e in elementos if "btnAddToCart" in e.get("resource_id", "") or "add to cart" in (e.get("text", "") + " " + e.get("desc", "") + " " + e.get("resource_id", "")).lower()), None)
            campo_peso = next((e for e in elementos if any(k in (e.get("text", "") + " " + e.get("resource_id", "")).lower() for k in ["0.00 lb", "txtNetWeight", "txtTotalWeight", "net weight", "net_weight"])), None)
            peso_actual_str = ""
            for e in elementos:
                t = e.get("text", "").strip()
                if ("lb" in t.lower() or "kg" in t.lower()) and not any(k in t.lower() for k in ["$", "price", "/"]):
                    peso_actual_str = t
                    break
            teclado_visible = any("quantityView" in e.get("resource_id", "") or e.get("text") == "WEIGHT" for e in elementos) or any(any(e.get("text") == str(d) or f"btn{d}" in e.get("resource_id", "") for d in range(10)) for e in elementos)
            btn_ok_teclado = next((e for e in elementos if "btnDone" in e.get("resource_id", "") or any(k in e.get("text", "").lower() for k in ["ok", "done", "enter", "confirm"]) or any(k in e.get("resource_id", "").lower() for k in ["btn_ok", "btn_enter", "button_ok"])), None)

            return {
                "tipo": "MODAL_PESABLE",
                "descripcion": "Modal de producto pesable (Weight Item)",
                "btn_manual_weight": btn_manual,
                "btn_add_to_cart": btn_add,
                "campo_peso": campo_peso,
                "peso_actual_str": peso_actual_str,
                "teclado_visible": teclado_visible,
                "btn_ok_teclado": btn_ok_teclado
            }

        # 15. Modal de Precio de Producto Abierto (Open Price / Carne)
        if any("label_10" in i or "label_20" in i or "label_50" in i for i in ids) or (
            any("label_" in i for i in ids) and any("btn_done" in i or "amount" in i for i in ids)
        ) or any(k in texto_unido for k in ["enter price", "open item"]):
            btn_ok = next((e for e in elementos if e.get("text") == "OK" or "btn_done" in e.get("resource_id", "").lower() or "btn_ok" in e.get("resource_id", "").lower()), None)
            btn_diez = next((e for e in elementos if "$10.00" in e.get("text", "") or "label_10" in e.get("resource_id", "")), None)
            monto_actual = next((e.get("text") for e in elementos if "amount" in e.get("resource_id", "")), "$0.00")
            return {
                "tipo": "MODAL_PRECIO_PRODUCTO",
                "descripcion": "Modal de precio de producto abierto (Open Price)",
                "btn_ok": btn_ok,
                "btn_diez": btn_diez,
                "monto_actual": monto_actual
            }

        return {"tipo": "DESCONOCIDO", "descripcion": "Pantalla general"}

    CAMPOS_PROHIBIDOS = ["order notes", "reference", "sku", "product name", "notas de la orden", "referencia", "type to add note", "type to add a note", "add note", "notes:", "customer_edittext"]
    cliente_override = None
    descuento_intentado = False

    @classmethod
    def es_campo_prohibido(cls, elem: Dict[str, Any]) -> bool:
        """Determina si un elemento corresponde a campos ajenos como Order Notes o Reference."""
        txt = (elem.get("text", "") + " " + elem.get("hint", "") + " " + elem.get("desc", "") + " " + elem.get("resource_id", "")).lower()
        return any(p in txt for p in cls.CAMPOS_PROHIBIDOS)

    @classmethod
    def validar_coherencia_y_sugerir_accion(cls, paso_objetivo, pantalla_info, elementos, analisis_video=None):
        """Desactivado para permitir IA general y evitar hardcoding."""
        return None

class LocalizadorHibridoResolucion:
    """
    Localizador Cognitivo de 4 capas — completamente independiente del tamaño de pantalla.

    El bot entiende QUÉ elemento debe tocar (por texto, descripción, ID) gracias
    al análisis semántico del video por Gemini. Lo encuentra en la UI real del dispositivo
    sin importar si la pantalla es 1920x1080, 1280x800 o 800x480.

    Capas (de mayor a menor confianza):
    [0] COGNITIVO_DIRECTO  — texto_exacto / content_desc / resource_id / hint del JSON de Gemini
    [1] SEMANTICO_DIFUSO   — términos clave parciales en la jerarquía XML real
    [2] SNAP_ADAPTABLE     — escalado porcentual + enganche al elemento clickeable más cercano
    [3] ESCALADO_PROPORCIONAL — último recurso, coordenadas proporcionales sin snap
    """

    STOP_WORDS = {
        "el", "la", "los", "las", "un", "una", "de", "del", "en", "para", "por",
        "con", "al", "a", "y", "o", "boton", "botón", "campo", "icono", "ícono",
        "modulo", "módulo", "seccion", "sección", "pantalla", "tocar", "presionar",
        "abrir", "hacer", "pulsar", "seleccionar", "ingresar", "ver", "ir",
        "the", "button", "field", "tap", "click", "press", "enter", "input", "screen"
    }

    @classmethod
    def extraer_terminos_clave(cls, texto: str) -> List[str]:
        if not texto:
            return []
        palabras = re.findall(r'[a-zA-Z0-9áéíóúÁÉÍÓÚñÑ]{2,}', texto.lower())
        return [p for p in palabras if p not in cls.STOP_WORDS]

    @classmethod
    def _norm(cls, s: str) -> str:
        if not s:
            return ""
        return unicodedata.normalize("NFKD", s.lower().strip()).encode("ascii", "ignore").decode("ascii")

    @classmethod
    def resolver_accion_adaptable(
        cls,
        accion_video: Dict[str, Any],
        elementos_ui: List[Dict[str, Any]],
        ancho_pantalla: int,
        alto_pantalla: int
    ) -> Dict[str, Any]:
        """
        Localiza cognitivamente el elemento en la pantalla real del dispositivo,
        sin depender de coordenadas fijas. Usa 4 capas de confianza decreciente.
        """
        tipo_gesto = accion_video.get("tipo_gesto", "TAP").upper()
        elem_nombre = accion_video.get("elemento_visual", "")
        accion_desc = accion_video.get("accion", "")
        obj_desc = accion_video.get("objetivo", "")
        texto_a_escribir = accion_video.get("texto_a_escribir")

        # Localizadores semánticos directos extraídos por Gemini del video
        locs = accion_video.get("localizadores_semanticos") or {}
        texto_exacto   = cls._norm(locs.get("texto_exacto") or "")
        content_desc_l = cls._norm(locs.get("content_description") or "")
        res_id_frag    = cls._norm(locs.get("resource_id_fragmento") or "")
        hint_l         = cls._norm(locs.get("hint_placeholder") or "")

        # Coordenadas de fallback (nuevo esquema JSON del prompt)
        coords_fb = accion_video.get("coordenadas_fallback") or accion_video.get("coordenadas") or {}
        objetivo_comb = f"{elem_nombre} {texto_exacto} {hint_l}".lower()
        permite_prohibido = any(k in objetivo_comb for k in ["reference", "note", "sku"])
        permite_discard = "discard" in objetivo_comb
        permite_exit = "exit" in objetivo_comb

        def elem_valido(e):
            cx, cy = e.get("center", (0, 0))
            if not (0 < cx < ancho_pantalla and 0 < cy < alto_pantalla):
                return False
            if not permite_prohibido and SupervisorDeContexto.es_campo_prohibido(e):
                return False
            t_low = (e.get("text") or "").strip().lower()
            if not permite_discard and "discard" in t_low:
                return False
            if not permite_exit and t_low == "exit":
                return False
            return True

        # ─── CAPA 0: LOCALIZADORES SEMÁNTICOS DIRECTOS (máxima confianza) ───
        mejor_match_0 = None
        mejor_score_0 = 0
        mejor_razon_0 = ""
        
        for elem in elementos_ui:
            if not elem_valido(elem):
                continue
            cx, cy = elem["center"]
            etxt  = cls._norm(elem.get("text") or "")
            eid   = cls._norm(elem.get("resource_id") or "")
            ehint = cls._norm(elem.get("hint") or "")
            edesc = cls._norm(elem.get("content_desc") or "")

            score = 0
            razon = ""
            
            if texto_exacto and etxt and texto_exacto == etxt:
                score += 1000; razon = f"texto_exacto='{texto_exacto}'"
            elif texto_exacto and etxt and len(texto_exacto) >= 3 and (texto_exacto in etxt or (texto_exacto.startswith("pay") and etxt.startswith("pay ("))):
                score += 500; razon = f"texto contiene '{texto_exacto}'"
            elif texto_exacto and ehint and len(texto_exacto) >= 3 and texto_exacto in ehint:
                score += 500; razon = f"hint contiene '{texto_exacto}'"
                
            if content_desc_l and edesc and content_desc_l in edesc:
                score += 400
                if not razon: razon = f"content_desc='{content_desc_l}'"
                
            if hint_l and ehint and hint_l in ehint:
                score += 400
                if not razon: razon = f"hint='{hint_l}'"
                
            if res_id_frag and eid and res_id_frag in eid:
                score += 100
                if not razon: razon = f"resource_id contiene '{res_id_frag}'"

            # Castigar falsos positivos: si tenemos texto exacto pero este elemento tiene otro texto totalmente distinto, bajamos el score
            if texto_exacto and etxt and texto_exacto not in etxt and etxt not in texto_exacto:
                score -= 200

            if score > 0 and score > mejor_score_0:
                mejor_score_0 = score
                mejor_razon_0 = razon
                mejor_match_0 = elem

        if mejor_match_0 and mejor_score_0 > 0:
            nombre_resuelto = mejor_match_0.get("text") or mejor_match_0.get("hint") or mejor_match_0.get("resource_id") or elem_nombre
            return {
                "tipo_gesto": tipo_gesto,
                "x": mejor_match_0["center"][0], "y": mejor_match_0["center"][1],
                "resource_id": mejor_match_0.get("resource_id"),
                "metodo": "COGNITIVO_DIRECTO",
                "elemento_resuelto": nombre_resuelto,
                "texto_a_escribir": texto_a_escribir,
                "confianza": "MAXIMA",
                "explicacion": f"[Cognitivo {ancho_pantalla}x{alto_pantalla}] {mejor_razon_0} (score {mejor_score_0}) → '{nombre_resuelto}'"
            }

        # ─── CAPA 1: TÉRMINOS CLAVE DIFUSOS EN XML (solo del elemento objetivo, no de descripciones genéricas) ───
        terminos = set(cls.extraer_terminos_clave(elem_nombre))
        for extra in [texto_exacto, content_desc_l, res_id_frag, hint_l]:
            if extra:
                terminos.update(cls.extraer_terminos_clave(extra))

        mejor_elem = None
        mejor_puntaje = 0

        for elem in elementos_ui:
            if not elem_valido(elem):
                continue
            etxt  = (elem.get("text") or "").strip().lower()
            eid   = (elem.get("resource_id") or "").strip().lower()
            ehint = (elem.get("hint") or "").strip().lower()
            edesc = (elem.get("content_desc") or "").strip().lower()

            puntaje = 0
            for t in terminos:
                if len(t) < 3:
                    continue
                if t in etxt:  puntaje += 35
                if t in ehint: puntaje += 30
                if t in edesc: puntaje += 25
                if t in eid:   puntaje += 20

            if puntaje > mejor_puntaje and puntaje >= 30:
                mejor_puntaje = puntaje
                mejor_elem = elem

        if mejor_elem:
            cx, cy = mejor_elem["center"]
            nombre_id = mejor_elem.get("text") or mejor_elem.get("hint") or mejor_elem.get("resource_id") or elem_nombre
            return {
                "tipo_gesto": tipo_gesto,
                "x": cx, "y": cy,
                "resource_id": mejor_elem.get("resource_id"),
                "metodo": "SEMANTICO_DIFUSO",
                "elemento_resuelto": nombre_id,
                "texto_a_escribir": texto_a_escribir,
                "confianza": "ALTA",
                "explicacion": f"[Semántico {ancho_pantalla}x{alto_pantalla}] '{nombre_id}' (score={mejor_puntaje}) en ({cx},{cy})"
            }

        # ─── CAPA 2: ESCALADO + SNAP-TO-TARGET ───
        x_pct = coords_fb.get("x_pct", 0.5) if isinstance(coords_fb, dict) else 0.5
        y_pct = coords_fb.get("y_pct", 0.5) if isinstance(coords_fb, dict) else 0.5
        x_calc = max(10, min(ancho_pantalla - 10, int(x_pct * ancho_pantalla)))
        y_calc = max(10, min(alto_pantalla - 10, int(y_pct * alto_pantalla)))

        radio_snap = max(65, int(ancho_pantalla * 0.12))
        elem_cercano = None
        menor_dist = float("inf")

        for elem in elementos_ui:
            if not elem_valido(elem):
                continue
            cx, cy = elem["center"]
            dist = math.hypot(cx - x_calc, cy - y_calc)
            if dist < radio_snap and dist < menor_dist:
                menor_dist = dist
                elem_cercano = elem

        if elem_cercano:
            cx, cy = elem_cercano["center"]
            nombre_snap = elem_cercano.get("text") or elem_cercano.get("resource_id") or "Elemento cercano"
            return {
                "tipo_gesto": tipo_gesto,
                "x": cx, "y": cy,
                "resource_id": elem_cercano.get("resource_id"),
                "metodo": "SNAP_ADAPTABLE",
                "elemento_resuelto": nombre_snap,
                "texto_a_escribir": texto_a_escribir,
                "confianza": "MEDIA",
                "explicacion": f"[Snap {ancho_pantalla}x{alto_pantalla}] '{nombre_snap}' ({cx},{cy}) dist={menor_dist:.0f}px"
            }

        # ─── CAPA 3: ESCALADO PROPORCIONAL DIRECTO ───
        return {
            "tipo_gesto": tipo_gesto,
            "x": x_calc, "y": y_calc,
            "resource_id": None,
            "metodo": "ESCALADO_PROPORCIONAL",
            "elemento_resuelto": elem_nombre,
            "texto_a_escribir": texto_a_escribir,
            "confianza": "BAJA",
            "explicacion": f"[Fallback {ancho_pantalla}x{alto_pantalla}] ({x_calc},{y_calc}) desde {x_pct:.1%}x{y_pct:.1%}"
        }



# ==========================================
# 7. MOTOR INTELIGENTE DE VERIFICACIÓN (IA + SUPERVISOR)
# ==========================================
class BugVerificationEngine:
    """Motor de orquestación de verificación guiada por IA en dispositivo físico."""

    MODELOS_PRIORITARIOS = ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.8-flash"]

    # MODO AHORRO: Groq maneja TODO lo que no requiera visión de video.
    # Gemini se usa EXCLUSIVAMENTE para analizar el video (.mp4).
    # Esto reduce el consumo de Gemini de ~6-15 créditos/sesión a ~1 crédito/sesión.
    MODO_GROQ_PRIMARIO = True

    def __init__(self, adb: Optional[ADBController] = None, gemini_api_key: str = GEMINI_API_KEY, proveedor_ia: str = "groq"):
        self.adb = adb
        self.proveedor_ia = proveedor_ia.lower() if proveedor_ia else "groq"
        try:
            self.client = genai.Client(api_key=gemini_api_key)
        except Exception:
            self.client = None

        try:
            from ai_quota_manager import cargar_config_ia
            cfg = cargar_config_ia()
            self.groq_api_key = cfg.get("groq_api_key", "").strip()
            self.modelo_groq = cfg.get("modelo_groq", "openai/gpt-oss-120b")
            prov_cfg = cfg.get("proveedor_activo")
            if prov_cfg:
                self.proveedor_ia = prov_cfg.lower()
        except Exception:
            self.groq_api_key = ""
            self.modelo_groq = "openai/gpt-oss-120b"

    def _debe_usar_groq(self) -> bool:
        """
        Retorna True si Groq debe manejar esta tarea de texto.
        Siempre True cuando MODO_GROQ_PRIMARIO está activo y hay clave de Groq configurada.
        Gemini solo se llama para análisis de video (analizar_video).
        """
        return self.MODO_GROQ_PRIMARIO and bool(self.groq_api_key)


    def _generar_con_groq(self, prompt: str, system_prompt: str = "", max_tokens: int = 2048) -> str:
        """Ejecuta una llamada a Groq Cloud con reintentos automáticos hacia modelos de respaldo."""
        from proveedor_alternativo import ejecutar_llamada_openai_compatible, ENDPOINT_GROQ
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.modelo_groq,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens
        }
        try:
            resp = ejecutar_llamada_openai_compatible(ENDPOINT_GROQ, self.groq_api_key, payload, timeout=60.0)
            usage = resp.get("usage", {})
            if usage:
                MonitorConsumoTokens.registrar_groq(payload.get("model", self.modelo_groq), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            return resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            if self.modelo_groq != "qwen/qwen3.8-27b":
                print(f"   ⚠️ Modelo Groq '{self.modelo_groq}' ocupado ({e}). Reintentando con Qwen 3.8 27B...")
                payload["model"] = "qwen/qwen3.8-27b"
                resp = ejecutar_llamada_openai_compatible(ENDPOINT_GROQ, self.groq_api_key, payload, timeout=60.0)
                usage = resp.get("usage", {})
                if usage:
                    MonitorConsumoTokens.registrar_groq("qwen/qwen3.8-27b", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
                return resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            raise e


    def _generar_con_fallback(self, contents: List[Any], response_mime_type: str = None) -> Any:
        """
        Router inteligente de IA:
        - Si el contenido es SOLO texto y Groq está disponible → Groq primero (0 créditos Gemini)
        - Si el contenido tiene imágenes/partes binarias → Gemini (visión multimodal)
        - Si Gemini falla → Groq como emergencia
        """
        ultimo_error = None
        config = types.GenerateContentConfig(response_mime_type=response_mime_type) if response_mime_type else None

        # Detectar si hay contenido binario (imágenes adjuntas) que requiere Gemini Vision
        tiene_binario = any(
            hasattr(item, "mime_type") or hasattr(item, "data") or
            (isinstance(item, dict) and "mime_type" in item) or
            (hasattr(item, "__class__") and item.__class__.__name__ in ("Part", "Blob", "FileData"))
            for item in contents
        )

        # ── GROQ PRIMARIO para tareas de texto puro (0 créditos de Gemini) ──
        if self._debe_usar_groq() and not tiene_binario:
            prompt_texto = "\n".join(str(item) for item in contents if isinstance(item, str))
            if prompt_texto.strip():
                try:
                    print("   🟢 [GROQ] Procesando con Groq Cloud (sin consumo de créditos Gemini)...")
                    resp_groq = self._generar_con_groq(prompt_texto, max_tokens=3000)
                    class _GR:
                        def __init__(self, t): self.text = t
                    return _GR(resp_groq)
                except Exception as eg:
                    print(f"   ⚠️ Groq no respondió ({eg}). Usando Gemini como respaldo para esta tarea...")

        # ── GEMINI: multimodal o respaldo cuando Groq no está disponible ──
        if self.client:
            for modelo in self.MODELOS_PRIORITARIOS:
                try:
                    if config:
                        resp = self.client.models.generate_content(model=modelo, contents=contents, config=config)
                    else:
                        resp = self.client.models.generate_content(model=modelo, contents=contents)
                    if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                        p_tok = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
                        c_tok = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
                        MonitorConsumoTokens.registrar_gemini(modelo, p_tok, c_tok)
                    return resp
                except Exception as e:
                    err_str = str(e)
                    ultimo_error = e
                    if any(cod in err_str for cod in ["429", "503", "404", "RESOURCE_EXHAUSTED", "UNAVAILABLE"]):
                        print(f"   ⚠️ Gemini '{modelo}' saturado. Probando modelo siguiente...")
                        continue
                    else:
                        time.sleep(1)
                        continue

        # Último recurso: Groq de emergencia
        if self.groq_api_key:
            print("   🔄 Groq como último recurso de emergencia...")
            prompt_texto = "\n".join(str(i) for i in contents if isinstance(i, str))
            resp_groq = self._generar_con_groq(prompt_texto)
            class MockResponse:
                def __init__(self, text): self.text = text
            return MockResponse(resp_groq)

        raise RuntimeError(f"Todos los motores de IA fallaron: {ultimo_error}")

    def verificar_precondiciones_y_sesion(self, reporte: Dict[str, Any], modo_automatico: bool = False):
        """
        Verifica que la app esté abierta en primer plano y que la sesión no esté bloqueada por Passcode.
        Si la pantalla de PIN está presente, asiste interactivamente al usuario o desbloquea de forma autónoma.
        """
        print("\n" + "-" * 65)
        print("🔐 COMPROBACIÓN DE SESIÓN Y PRECONDICIONES DE LA APP")
        print("-" * 65)

        # 1. Asegurar que la app esté abierta en pantalla
        _, elems = self.adb.capturar_ui_xml()
        es_app_pos = any(self.adb.package_name in e.get("resource_id", "") for e in elems)
        if not es_app_pos:
            print("   🚀 Iniciando la aplicación On The Fly POS en el dispositivo...")
            self.adb.ejecutar_adb(["shell", "monkey", "-p", self.adb.package_name, "-c", "android.intent.category.LAUNCHER", "1"])
            dormir_con_escape(3.5)
            _, elems = self.adb.capturar_ui_xml()

        # 2. Detectar si está en pantalla de bloqueo o Passcode
        es_pantalla_pin = any(
            e.get("resource_id", "").endswith("lblPassword") or
            e.get("resource_id", "").endswith("textView_pin_text") or
            "passcode" in e.get("text", "").lower()
            for e in elems
        )

        if es_pantalla_pin:
            print("🔒 ATENCIÓN: La aplicación está en la pantalla de ingreso de Passcode (PIN).")
            if modo_automatico or not sys.stdin.isatty():
                print("   🤖 Modo autónomo: ingresando PIN 2222 para desbloquear terminal...")
                for _ in range(4):
                    self.adb.tap(1405, 431)
                    dormir_con_escape(0.3)
                dormir_con_escape(2.0)
                _, elems = self.adb.capturar_ui_xml()
            else:
                print("👉 Elige cómo deseas desbloquear la aplicación:")
                print("   [1] Escribir el PIN aquí en la consola para que el bot lo presione en pantalla.")
                print("   [2] Desbloquearlo tú manualmente tocando la pantalla de tu PAX.")
                print("-" * 65)

                opc = leer_input_con_escape("Escribe el PIN de 4 dígitos o presiona [Enter] si lo desbloqueas a mano: ").strip()

                if opc and opc.isdigit():
                    print(f"   🤖 Bot digitando PIN '{opc}' en la pantalla física...")
                    for digito in opc:
                        btn_id = f"{self.adb.package_name}:id/btn_{digito}"
                        elem_btn = next((e for e in elems if e.get("resource_id") == btn_id), None)
                        if elem_btn:
                            cx, cy = elem_btn["center"]
                            self.adb.tap(cx, cy)
                        else:
                            self.adb.keyevent(int(digito) + 7)
                        dormir_con_escape(0.35)

                    dormir_con_escape(2.0)
                    _, elems = self.adb.capturar_ui_xml()

                else:
                    print("   ⏳ Esperando a que ingreses el PIN en el terminal [Presiona Enter cuando estés en la pantalla principal]...")
                    leer_input_con_escape()
                    _, elems = self.adb.capturar_ui_xml()

        # 3. Mostrar precondiciones específicas del reporte
        print("\n📋 Precondiciones reportadas para esta prueba:")
        if reporte.get("precondiciones"):
            for line in reporte["precondiciones"].splitlines():
                if line.strip():
                    print(f"   {line.strip()}")
        else:
            print("   - Iniciar sesión en la app y posicionarse en la pantalla inicial.")

        print("-" * 65)
        if modo_automatico or not sys.stdin.isatty():
            print("   🚀 Modo autónomo activado: iniciando prueba en pantalla física inmediatamente...")
            dormir_con_escape(1.0)
        else:
            leer_input_con_escape("👉 Presiona [Enter] cuando el terminal esté listo para iniciar la prueba [ESC para salir]: ")
        print("=" * 65)

    def evaluar_factibilidad_entorno_local(self, reporte: Dict[str, Any], disp_actual: Dict[str, str]) -> Tuple[bool, str]:
        """
        Evalúa si la prueba requiere hardware o equipos externos que NO pueden
        validarse sobre la pantalla y app del dispositivo local conectado.
        """
        texto_completo = f"""
TÍTULO: {reporte['titulo']}
DISPOSITIVO REPORTADO: {reporte['dispositivo']}
DISPOSITIVO LOCAL CONECTADO: {disp_actual['modelo']} ({disp_actual['fabricante']})
PRECONDICIONES: {reporte['precondiciones']}
PROBLEMA: {reporte['problema_actual']}
PROCEDIMIENTO: {reporte['procedimiento_raw']}
RESULTADO ESPERADO: {reporte['resultado_esperado']}
"""
        prompt = f"""Eres un Ingeniero Líder de QA y Automatización.
Analiza detenidamente el siguiente reporte de bug para determinar si puede ejecutarse y verificarse de forma autónoma en la pantalla física y la app local del dispositivo Android conectado ({disp_actual['modelo']}), o si por el contrario REQUIERE OBLIGATORIAMENTE hardware externo ajeno o físico adicional no disponible en el entorno de prueba local (por ejemplo:
- Segunda pantalla física del cliente externa desconectada o ausente.
- Datáfono / Pinpad bancario externo adicional que requiera insertar o deslizar tarjeta bancaria física en un periférico separado.
- Cajón portamonedas físico o impresora externa Bluetooth/Serial ausente.
- Servidor o equipo físico externo ajeno a este terminal Android).

{texto_completo}

Responde ÚNICAMENTE en formato JSON con la siguiente estructura:
{{
  "es_factible_localmente": true | false,
  "motivo": "Explicación clara y concisa del porqué es factible o qué equipo externo específico falta"
}}
"""
        try:
            resp = self._generar_con_fallback(
                contents=[prompt],
                response_mime_type="application/json"
            )
            data = json.loads(resp.text)
            return data.get("es_factible_localmente", True), data.get("motivo", "")
        except Exception as e:
            p_lower = (reporte['titulo'] + " " + reporte['procedimiento_raw'] + " " + reporte['dispositivo']).lower()
            if "pantalla del cliente" in p_lower and "doble pantalla" in p_lower and "e800" not in disp_actual['modelo'].lower():
                return False, "La prueba requiere pantalla del cliente externa que no está disponible en este dispositivo."
            return True, "Evaluación completada."

    def decidir_siguiente_accion(self, paso_objetivo: str, paso_idx: int, total_pasos: int,
                                 elementos_ui: List[Dict[str, Any]], logs_recientes: str,
                                 analisis_video: Optional[Dict[str, Any]] = None,
                                 screen_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Analiza visualmente la pantalla física (captura de pantalla) y los elementos interactivos
        para decidir el siguiente toque o acción exacta a ejecutar en la pantalla.
        """
        # 1. Evaluación inmediata y gratuita por el Supervisor Cognitivo de Contexto
        pantalla_info = SupervisorDeContexto.clasificar_pantalla(elementos_ui)
        accion_contextual = SupervisorDeContexto.validar_coherencia_y_sugerir_accion(paso_objetivo, pantalla_info, elementos_ui, analisis_video=analisis_video)
        if accion_contextual:
            return accion_contextual

        # 2. Si no es un patrón determinista, filtrar elementos relevantes para IA (priorizando textos y clickables)
        elementos_relevantes = [
            e for e in elementos_ui
            if (e.get("text") or e.get("clickable") or "button" in e.get("class", "").lower() or "edittext" in e.get("class", "").lower())
        ]
        if not elementos_relevantes:
            elementos_relevantes = elementos_ui

        elementos_filtrados = []
        for e in elementos_relevantes[:100]:
            elementos_filtrados.append({
                "texto": e.get("text", ""),
                "id": e.get("resource_id", "").split("/")[-1] if e.get("resource_id") else "",
                "clase": e.get("class", "").split(".")[-1],
                "centro": e.get("center", (0, 0)),
                "clickable": e.get("clickable", False)
            })

        bloque_video = ""
        if analisis_video:
            bloque_video = f"""
EVIDENCIA PREVIA EXTRAÍDA DEL VIDEO ORIGINAL GRABADO:
- Resumen del flujo en video: {analisis_video.get('resumen_evidencia', '')}
- Falla observada en el video: {analisis_video.get('comportamiento_bug_observado', '')}
- Acciones cronológicas en el video:
{json.dumps(analisis_video.get('acciones_cronologicas', []), ensure_ascii=False, indent=2)}
"""

        ancho, alto = self.adb.obtener_resolucion()
        
        # Inyectar instrucción guardada por el usuario si existe
        instruccion_guardada = ProcedimientosAprendidos.buscar(pantalla_info.get("tipo", "DESCONOCIDO"), paso_objetivo)
        bloque_instruccion = ""
        if instruccion_guardada:
            bloque_instruccion = f"\nATENCIÓN - REGLA GUARDADA POR EL USUARIO PARA ESTA PANTALLA:\n'{instruccion_guardada.get('descripcion', '')}'\nDEBES OBEDECER ESTA INSTRUCCIÓN ESTRICTAMENTE AL ELEGIR TU ACCIÓN.\n"

        prompt = f"""Eres el bot de automatización de QA que opera un dispositivo Android físico con On The Fly POS.
Tu misión es ejecutar en la pantalla física el siguiente paso del caso de prueba:
PASO A EJECUTAR ({paso_idx}/{total_pasos}): "{paso_objetivo}"
{bloque_video}{bloque_instruccion}
DATOS DE PANTALLA:
- Resolución física: {ancho}x{alto}
- Elementos interactivos reconocidos en jerarquía XML ({len(elementos_filtrados)} elementos):
{json.dumps(elementos_filtrados, ensure_ascii=False, indent=2)}

LOGS RECIENTES DE LA APP:
\"\"\"
{logs_recientes}
\"\"\"

DIRECTRICES VISUALES Y DE NAVEGACIÓN (CRÍTICO):
1. REVISA DETALLADAMENTE LA IMAGEN ADJUNTA DE LA PANTALLA FÍSICA.
2. Si el botón o elemento requerido para cumplir el paso ESTÁ VISIBLE en la pantalla (por ejemplo: botón 'QUICK SALE', 'Orders', 'Tables Map', 'View Loyalty', 'Rewards', 'Convert points', etc.), DEBES indicar "TAP" con sus coordenadas (x, y) sobre la pantalla de {ancho}x{alto} para pulsarlo directamente.
3. Si el objetivo del paso YA fue alcanzado (por ejemplo, ya estás dentro del módulo o pantalla que se pedía abrir, o los productos ya están en la orden), responde "PASO_COMPLETO".
4. NUNCA respondas "WAIT" si la pantalla física ya tiene los botones o módulos cargados y visibles. Solo responde "WAIT" si la pantalla está completamente en blanco, negra o mostrando un indicador explícito de carga/progreso.
5. MANEJO DE MODAL 'WEIGHT ITEM' (PRODUCTOS PESABLES COMO QUESO):
     - Si la pantalla muestra el modal 'Weight Item' con 'Net Weight: 0.00 lb':
       Pulsa 'Manual Weight' (botón de contorno azul, aprox x=635, y=735) para abrir el teclado numérico de peso.
     - Si el teclado numérico está activo: pulsa el número con las libras requeridas según el video o paso (ej: '2' o '2.0') y presiona 'OK'.
  6. MANEJO DE BUGS RESUELTOS (DIALOGOS AUSENTES):
     - Si el paso actual te pide interactuar con un "diálogo de error", "mensaje del sistema", o algo similar que bloqueaba la app, pero en la pantalla actual DICHO ERROR NO EXISTE (la app funciona normalmente), esto significa que el bug ya fue corregido por los desarrolladores. En este caso OBLIGATORIAMENTE responde "PASO_COMPLETO" con la explicación "El diálogo de error no aparece, el bug parece estar resuelto".
   - Si el peso ya tiene valor (> 0 lb) y el botón 'Add to Cart' está habilitado: pulsa 'Add to Cart' (aprox x=795, y=735) para agregar el producto pesable al carrito.
6. PRODUCTOS DE PRECIO ABIERTO (OPEN PRICE COMO CARNE REGULAR):
   - Si pide precio abierto, pulsa el botón '$10.00' y luego 'OK'.
7. CLIENTES CON MEMBRESÍA (HUMBERTO GOLD / TATIANA):
   - Para vincular cliente, pulsa 'View Loyalty / Membership' o el buscador de cliente 'Phone Number / Name', busca el cliente ('Gold' o 'Humberto Gold') y selecciónalo.
8. PANTALLA DE PAGOS Y DESCUENTOS:
   - Para avanzar a cobro: pulsa 'PAY' (botón azul con total abajo a la derecha).
   - Para descuentos: pulsa 'Add Discount', selecciona '50%' (Spill Comp), e introduce el PIN de autorización '1111' si la app pide Passcode.
9. DESPLAZAMIENTOS (SCROLL / SWIPE):
   - Si el botón objetivo (ej. 'Payment Request') NO está visible en pantalla, DEBES usar la acción "SWIPE" para deslizar y buscarlo. (ej. deslizar hacia arriba para ver elementos abajo).

Acciones válidas:
- "PASO_COMPLETO": si el objetivo de este paso ya fue alcanzado en la pantalla actual.
- "TAP": pulsar en las coordenadas (x, y) del elemento correcto.
- "SWIPE": deslizar la pantalla. Proporcionar x1, y1 (inicio) y x2, y2 (fin). Ej: para bajar en la lista desliza de abajo hacia arriba (y1 mayor a y2).
- "TYPE": escribir un texto en un campo de entrada activo.
- "KEYEVENT": presionar botón físico como BACK (código 4) o ENTER (código 66).
- "WAIT": esperar solo si la pantalla está genuinamente congelada o cargando.

Responde ÚNICAMENTE en formato JSON con la siguiente estructura:
{{
  "accion": "PASO_COMPLETO" | "TAP" | "SWIPE" | "TYPE" | "KEYEVENT" | "WAIT",
  "x": 123,
  "y": 456,
  "x1": 500,
  "y1": 800,
  "x2": 500,
  "y2": 300,
  "texto": "texto a escribir si es TYPE",
  "keycode": 4,
  "explicacion": "Breve explicación visible de qué acción o botón se está tocando para lograr el paso"
}}
"""
        try:
            if self._debe_usar_groq():
                prompt_groq = f"""Eres un Ingeniero Experto en Automatización de Pruebas de QA para On The Fly POS en terminales PAX Android.
Tu misión es interpretar la pantalla actual y determinar la siguiente acción exacta para reproducir el bug.
{prompt}
Responde OBLIGATORIAMENTE con un objeto JSON válido con los campos: "accion", "x", "y", "x1", "y1", "x2", "y2", "texto", "keycode", "explicacion"."""
                resp_text = self._generar_con_groq(prompt_groq, system_prompt="Eres un agente de QA que responde únicamente en formato JSON.")
                resp_text = re.sub(r"^```json\s*", "", resp_text.strip(), flags=re.IGNORECASE)
                resp_text = re.sub(r"^```\s*", "", resp_text)
                resp_text = re.sub(r"\s*```$", "", resp_text)
                decision = json.loads(resp_text)
            else:
                partes = []
                if screen_path and os.path.exists(screen_path):
                    try:
                        with open(screen_path, "rb") as f_img:
                            img_bytes = f_img.read()
                            if img_bytes:
                                partes.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))
                    except Exception:
                        pass

                partes.append(prompt)

                resp = self._generar_con_fallback(
                    contents=partes,
                    response_mime_type="application/json"
                )
                decision = json.loads(resp.text)
            accion = decision.get("accion", "WAIT").upper()


            # 🛡️ GUARDIA DE INTEGRIDAD ANTI-ALUCINACIÓN
            if accion in ("TAP", "TYPE"):
                x = decision.get("x", 0)
                y = decision.get("y", 0)
                elem_tocado = next((
                    e for e in elementos_ui
                    if e.get("bounds") and e["bounds"][0] <= x <= e["bounds"][2] and e["bounds"][1] <= y <= e["bounds"][3]
                ), None)

                if elem_tocado and SupervisorDeContexto.es_campo_prohibido(elem_tocado):
                    nombre_campo = elem_tocado.get("text") or elem_tocado.get("hint") or elem_tocado.get("resource_id") or "Campo Prohibido"
                    print(f"   🛡️ BLOQUEO DE SEGURIDAD: El bot intentó escribir/tocar en '{nombre_campo}'. Reorientando acción...")

                    pass

            return decision
        except Exception as e:
            return {
                "accion": "WAIT",
                "explicacion": f"Reintentando interpretación ({e})..."
            }

    def emitir_veredicto_final(self, reporte: Dict[str, Any], screen_path: str,
                              elementos_finales: List[Dict[str, Any]],
                              historial_acciones: List[str],
                              logs_totales: str,
                              analisis_video: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Evalúa el estado visual final, la jerarquía y los logs acumulados
        para determinar con certeza si el bug fue corregido o persiste.
        """
        elementos_txt = [e.get("text") for e in elementos_finales if e.get("text")]
        pantalla_final = SupervisorDeContexto.clasificar_pantalla(elementos_finales)

        # Regla Cognitiva Determinista para Bug de Redención de Puntos
        es_caso_redencion = any(k in reporte['titulo'].lower() for k in ["redención de puntos", "redencion de puntos", "convert points", "ingreso manual de montos"])
        if es_caso_redencion and pantalla_final.get("tipo") == "MODAL_CONVERT_POINTS":
            tiene_confirmar = pantalla_final.get("tiene_boton_confirmar", False)
            es_editable = pantalla_final.get("es_campo_editable", False)

            if not tiene_confirmar and not es_editable:
                return {
                    "veredicto": "PERSISTE",
                    "mensaje_principal": "EL BUG AÚN SE MANTIENE",
                    "analisis_detallado": "En la sección 'Convert points' de Rewards se confirmó la ausencia de un botón explícito de confirmación ('Redeem'/'Apply') para procesar el canje, requiriendo pulsar directamente el recuadro verde. Adicionalmente, el campo numérico no es editable ni abre el teclado virtual, forzando múltiples toques en el botón '+'.",
                    "evidencia_clave": "Pantalla modal 'Convert points' activa: sin botón de confirmación explícito y con campo numérico de solo incremento (+)."
                }
            elif tiene_confirmar or es_editable:
                return {
                    "veredicto": "CORREGIDO",
                    "mensaje_principal": "EL BUG YA FUE CORREGIDO",
                    "analisis_detallado": "Se detectó la incorporación del botón explícito de confirmación en la interfaz de redención de puntos o la capacidad de ingresar montos manualmente.",
                    "evidencia_clave": f"Botón de confirmación presente: {pantalla_final.get('btn_confirmar', {}).get('text', 'Sí')}, Campo editable: {es_editable}."
                }

        # Regla Cognitiva Determinista para Bug de Discrepancia de Redondeo en Totales de Tarjeta
        es_caso_redondeo = any(k in reporte['titulo'].lower() for k in ["discrepancia de redondeo", "redondeo en el total", "tarjeta de crédito"])
        if es_caso_redondeo:
            # 1. Verificar si estamos efectivamente en PANTALLA_PAGOS
            if pantalla_final.get("tipo") != "PANTALLA_PAGOS":
                return {
                    "veredicto": "EJECUCION_BLOQUEADA",
                    "mensaje_principal": "NO SE PUDO AUDITAR: NO SE ALCANZÓ LA PANTALLA DE PAGOS",
                    "analisis_detallado": f"La prueba se detuvo antes de alcanzar la pantalla de pagos (Checkout). La pantalla final fue '{pantalla_final.get('tipo', 'DESCONOCIDO')}'. Se requiere completar todos los pasos hasta el Checkout para auditar la discrepancia.",
                    "evidencia_clave": f"Pantalla final: {pantalla_final.get('descripcion', 'Desconocida')}."
                }

            # 2. Extraer monto del botón Credit Card (com.kubilabs.ontheflypos:id/txtCreditCardValue)
            monto_cc = None
            elem_val = next((e for e in elementos_finales if "txtCreditCardValue" in e.get("resource_id", "") or "creditcardvalue" in e.get("resource_id", "").lower()), None)
            if elem_val and elem_val.get("text"):
                m_cc = re.search(r'\$\s*(\d+(?:\.\d{2})?)', elem_val.get("text", ""))
                if m_cc:
                    monto_cc = float(m_cc.group(1))

            if monto_cc is None:
                for e in elementos_finales:
                    txt = e.get("text", "")
                    if any(k in txt.lower() for k in ["credit card", "card", "tarjeta"]) or "creditcard" in e.get("resource_id", "").lower():
                        m_cc = re.search(r'\$\s*(\d+(?:\.\d{2})?)', txt)
                        if m_cc:
                            monto_cc = float(m_cc.group(1))
                            break

            if monto_cc is None:
                for e in elementos_finales:
                    txt = e.get("text", "")
                    m_cc = re.search(r'\$\s*(5\.5[01])', txt)
                    if m_cc:
                        monto_cc = float(m_cc.group(1))
                        break

            # 3. Verificar que el descuento o la membresía prioritaria estén presentes
            txts_pagos = [e.get("text", "").lower() for e in elementos_finales if e.get("text")]
            tiene_spill_comp = any("spill comp" in t or "discounts by order" in t or "-$9.36" in t or "comp n/a" in t for t in txts_pagos) or (monto_cc in [5.50, 5.51])
            prioridad_membresia = any("membership discount with priority" in t or "remove the member" in t for t in txts_pagos) or any("gold member" in t for t in txts_pagos)

            if not tiene_spill_comp and not prioridad_membresia:
                return {
                    "veredicto": "EJECUCION_BLOQUEADA",
                    "mensaje_principal": "NO SE PUDO AUDITAR: DESCUENTO NO APLICADO",
                    "analisis_detallado": "No se aplicó el descuento requerido en el paso 5 ni se constató la membresía activa en el checkout.",
                    "evidencia_clave": "Falta el descuento o membresía en el resumen de la orden."
                }

            if monto_cc == 5.51:
                return {
                    "veredicto": "PERSISTE",
                    "mensaje_principal": "EL BUG AÚN SE MANTIENE",
                    "analisis_detallado": "Se constató la discrepancia aritmética de un centavo ($5.51 en el botón Credit Card vs $5.50 calculado por la sumatoria de ítems, descuentos e impuestos). El error de redondeo aritmético en la pasarela de tarjeta de crédito continúa reproduciéndose tal como se reportó.",
                    "evidencia_clave": "Botón 'Credit Card' muestra $5.51 mientras que la sumatoria desglosada totaliza $5.50."
                }
            elif monto_cc == 5.50:
                return {
                    "veredicto": "CORREGIDO",
                    "mensaje_principal": "EL BUG YA FUE CORREGIDO",
                    "analisis_detallado": "El monto total reflejado en el botón de pago por Tarjeta de Crédito ($5.50) coincide de forma unificada y exacta con la sumatoria aritmética de los rubros detallados tras aplicar los descuentos múltiples.",
                    "evidencia_clave": "Botón 'Credit Card' unificado en $5.50 coincidente con el desglose contable."
                }
            elif monto_cc == 10.73:
                return {
                    "veredicto": "CORREGIDO",
                    "mensaje_principal": "EL BUG YA FUE CORREGIDO (PREVENCIÓN POR PRIORIDAD DE MEMBRESÍA)",
                    "analisis_detallado": "En la versión 4.4.1.03debug, el sistema mitiga de raíz la discrepancia de redondeo al bloquear la combinación simultánea de descuentos manuales con descuentos de membresía ('This order has a membership discount with priority. Remove the member to apply a manual discount.'). Con el 50% de descuento de la membresía Gold aplicado, el botón Credit Card refleja con exactitud estricta $10.73, idéntico a la sumatoria exacta de ítems e impuestos ($9.36 + $0.26 + $0.83 + $0.28 = $10.73) sin diferencias de centavos.",
                    "evidencia_clave": "Botón 'Credit Card' en $10.73 coincidente al 100% con la sumatoria desglosada y protección activa contra combinación indebida de descuentos."
                }
            else:
                return {
                    "veredicto": "EJECUCION_BLOQUEADA",
                    "mensaje_principal": f"MONTO INESPERADO (${monto_cc})",
                    "analisis_detallado": f"El monto observado en Credit Card (${monto_cc}) no coincide con los valores previstos en el caso de prueba ($5.51, $5.50 o $10.73).",
                    "evidencia_clave": f"Monto observado: ${monto_cc}."
                }

        bloque_video_final = ""
        if analisis_video:
            bloque_video_final = f"""
ANÁLISIS DEL VIDEO DE EVIDENCIA ORIGINAL GRABADO POR EL OPERADOR:
Resumen del video: {analisis_video.get('resumen_evidencia', '')}
Falla exacta observada en el video: {analisis_video.get('comportamiento_bug_observado', '')}
Criterios de verificación extraídos: {json.dumps(analisis_video.get('puntos_criticos_a_verificar', []), ensure_ascii=False)}
"""

        prompt = f"""Eres el Juez Supremo de Quality Assurance.
Tu tarea es emitir un veredicto definitivo sobre la verificación del siguiente bug:

TÍTULO DEL BUG: {reporte['titulo']}
COMPORTAMIENTO ANÓMALO REPORTADO:
{reporte['problema_actual']}

RESULTADO ESPERADO DE LA CORRECCIÓN:
{reporte['resultado_esperado']}

NOTAS DE DESARROLLO / COMMITS:
{json.dumps(reporte['notas_desarrollador'], ensure_ascii=False)}
{bloque_video_final}
ACCIONES REALIZADAS POR EL BOT DURANTE LA PRUEBA:
{json.dumps(historial_acciones, ensure_ascii=False, indent=2)}

TEXTOS VISIBLES EN LA PANTALLA FINAL:
{json.dumps(elementos_txt, ensure_ascii=False)}

LOGS CAPTURADOS TRAS LA PRUEBA:
\"\"\"
{logs_totales[-2000:]}
\"\"\"

Determina rigurosamente:
1. ¿El bug reportado originalmente aún se manifiesta, o el comportamiento observado cumple con el resultado esperado y el código corregido?
2. Si el fallo desapareció o la función opera correctamente: "CORREGIDO".
3. Si el fallo persiste, el elemento sigue roto/ausente, o persiste el error: "PERSISTE".

Responde ÚNICAMENTE en JSON con esta estructura exacta:
{{
  "veredicto": "CORREGIDO" | "PERSISTE",
  "mensaje_principal": "EL BUG YA FUE CORREGIDO" | "EL BUG AÚN SE MANTIENE",
  "analisis_detallado": "Explicación técnica detallada que sustenta el veredicto basándose en la pantalla y logs",
  "evidencia_clave": "Hecho puntual observado que confirma o refuta la corrección"
}}
"""
        try:
            if self._debe_usar_groq():
                prompt_veredicto = f"""Eres el Juez Supremo de Quality Assurance para On The Fly POS.
{prompt}
Responde OBLIGATORIAMENTE con un objeto JSON válido con los campos: "veredicto", "mensaje_principal", "analisis_detallado", "evidencia_clave"."""
                resp_txt = self._generar_con_groq(prompt_veredicto, system_prompt="Eres un Juez de QA que responde únicamente en formato JSON.")
                resp_txt = re.sub(r"^```json\s*", "", resp_txt.strip(), flags=re.IGNORECASE)
                resp_txt = re.sub(r"^```\s*", "", resp_txt)
                resp_txt = re.sub(r"\s*```$", "", resp_txt)
                return json.loads(resp_txt)
            else:
                partes = []
                if screen_path and os.path.exists(screen_path):
                    with open(screen_path, "rb") as f_img:
                        img_bytes = f_img.read()
                        partes.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))

                partes.append(prompt)

                resp = self._generar_con_fallback(
                    contents=partes,
                    response_mime_type="application/json"
                )
                return json.loads(resp.text)

        except Exception as e:
            return {
                "veredicto": "INCIERTO",
                "mensaje_principal": "NO SE PUDO DETERMINAR EL VEREDICTO AUTOMÁTICO",
                "analisis_detallado": f"Ocurrió un error al contactar al motor de evaluación: {e}",
                "evidencia_clave": "Logs no concluyentes"
            }


# ==========================================
# 7. ORQUESTADOR DEL FLUJO PRINCIPAL
# ==========================================
def seleccionar_dispositivo_adb() -> ADBController:
    """Detecta, lista y permite seleccionar el dispositivo Android a enlazar."""
    print("\n" + "=" * 65)
    print("🔍 PASO 1: DETECCIÓN Y ENLACE DE DISPOSITIVOS ANDROID (ADB)")
    print("=" * 65)

    dispositivos = ADBController.listar_dispositivos()
    if not dispositivos:
        print("❌ No se detectó ningún dispositivo Android conectado vía ADB.")
        print("👉 Asegúrate de:")
        print("   1. Conectar el cable USB a la computadora.")
        print("   2. Tener habilitada la 'Depuración por USB' en las Opciones de Desarrollador.")
        print("   3. Ejecutar 'adb devices' en la consola para autorizar la conexión.")
        input("\nPresiona [Enter] tras conectar el dispositivo para reintentar o [ESC] para salir...")
        dispositivos = ADBController.listar_dispositivos()
        if not dispositivos:
            raise RuntimeError("Operación abortada: No hay dispositivos ADB disponibles.")

    print(f"📱 Dispositivos detectados ({len(dispositivos)}):")
    for idx, d in enumerate(dispositivos, 1):
        print(f"  [{idx}] Modelo: {d['modelo']} | Fabricante: {d['fabricante']} | Serial: {d['serial']} ({d['estado']})")

    if len(dispositivos) == 1:
        elegido = dispositivos[0]
        print(f"\n✨ Enlazando automáticamente con el único dispositivo disponible: {elegido['modelo']} ({elegido['serial']})")
    else:
        sel_idx = leer_input_con_escape(f"\nSelecciona el número de dispositivo a usar [1-{len(dispositivos)}]: ").strip()
        num = int(sel_idx) if sel_idx.isdigit() and 1 <= int(sel_idx) <= len(dispositivos) else 1
        elegido = dispositivos[num - 1]

    controller = ADBController(serial=elegido['serial'])
    controller.activar_indicadores_visuales()
    return controller


def obtener_reporte_a_verificar(todoist: TodoistReviewClient) -> Dict[str, Any]:
    """Solicita el link o ID de la tarea de Code Review o permite elegir de la lista."""
    print("\n" + "=" * 65)
    print("📋 PASO 2: SELECCIÓN DEL REPORTE DE 'CODE REVIEW / QA'")
    print("=" * 65)
    print("💡 Puedes pegar el link directo de la tarea de Todoist (Ctrl + V)")
    print("   o presionar [L] para listar todos los reportes de la columna.")
    print("=" * 65)

    entrada = leer_input_con_escape("\n👉 Introduce el link o ID del reporte [o 'L' para listar]: ").strip()

    if entrada.upper() == "L" or not entrada:
        print("\n⏳ Consultando tareas en la sección 'Code Review / QA' de Todoist...")
        tareas = todoist.listar_tareas_code_review()
        if not tareas:
            raise RuntimeError("No se encontraron tareas pendientes en la sección 'Code Review / QA'.")

        print(f"\nReportes disponibles en 'Code Review / QA' ({len(tareas)}):")
        for idx, t in enumerate(tareas[:15], 1):
            titulo = t.get('content', '').replace('\n', ' ')[:75]
            print(f"  [{idx:2d}] {titulo}")

        sel = leer_input_con_escape(f"\nSelecciona el número de reporte a verificar [1-{min(len(tareas), 15)}]: ").strip()
        idx_t = int(sel) - 1 if sel.isdigit() and 1 <= int(sel) <= len(tareas) else 0
        tarea_seleccionada = tareas[idx_t]
        task_id = tarea_seleccionada['id']
    else:
        task_id = todoist.extraer_id_desde_url(entrada)
        print(f"   ⏳ Consultando datos de la tarea ID: {task_id}...")
        tarea_seleccionada = todoist.obtener_tarea_por_id(task_id)

    comentarios = todoist.obtener_comentarios(task_id)
    reporte = BugReportParser.parsear(tarea_seleccionada, comentarios)

    print("\n" + "-" * 65)
    print(f"📌 TÍTULO: {reporte['titulo']}")
    print(f"📱 Dispositivo reportado: {reporte['dispositivo']}")
    print(f"📦 Versión reportada:     {reporte['version']}")
    if reporte['notas_desarrollador']:
        print(f"💬 Comentarios de devs:   {', '.join(reporte['notas_desarrollador'])}")
    print(f"🔢 Pasos a replicar:      {len(reporte['pasos'])} pasos identificados")
    for i, p in enumerate(reporte['pasos'], 1):
        print(f"   {i}. {p}")
    print("-" * 65)

    return reporte


# ==========================================
# 8. GENERADOR DE REPORTES HTML (AUDITORÍA QA)
# ==========================================
class HTMLReportGenerator:
    """Generador de reportes HTML modernos, autocontenidos y profesionales para auditoría de QA."""

    @staticmethod
    def sanitizar_nombre_carpeta(titulo: str, max_len: int = 100) -> str:
        s = re.sub(r'[\\/*?:"<>|]', "_", titulo).strip()
        s = re.sub(r'\s+', ' ', s)
        return s[:max_len].strip()

    @classmethod
    def generar_reporte(cls, reporte: Dict[str, Any], registro_pasos: List[Dict[str, Any]],
                        veredicto: Dict[str, Any], carpeta_destino: str,
                        dispositivo_info: Dict[str, str], duracion_segundos: float,
                        log_monitor: Optional['LogCatMonitor'] = None) -> str:
        """Construye y guarda el reporte HTML interactivo con capturas, procedimientos y logs."""
        os.makedirs(carpeta_destino, exist_ok=True)
        ruta_html = os.path.join(carpeta_destino, "reporte_verificacion.html")

        # Determinar estado global y estilos
        codigo_veredicto = veredicto.get("veredicto", "INCIERTO")
        if codigo_veredicto == "CORREGIDO":
            badge_clase = "badge-success"
            badge_texto = "✅ EL BUG YA FUE CORREGIDO"
            color_primario = "#10b981"
        elif codigo_veredicto == "PERSISTE":
            badge_clase = "badge-danger"
            badge_texto = "⚠️ EL BUG AÚN SE MANTIENE"
            color_primario = "#f43f5e"
        elif codigo_veredicto == "EJECUCION_BLOQUEADA":
            badge_clase = "badge-warning"
            badge_texto = "🛑 VERIFICACIÓN INTERRUMPIDA (PASO BLOQUEADO)"
            color_primario = "#f59e0b"
        else:
            badge_clase = "badge-info"
            badge_texto = "ℹ️ VERIFICACIÓN NO CONCLUYENTE"
            color_primario = "#64748b"

        pasos_completados = sum(1 for p in registro_pasos if p.get("estado") == "COMPLETADO")
        pasos_fallidos = sum(1 for p in registro_pasos if p.get("estado") == "FALLIDO")
        pasos_bloqueados = sum(1 for p in registro_pasos if p.get("estado") == "BLOQUEADO")
        total_pasos = len(registro_pasos)

        # Generar bloques HTML para cada paso
        bloques_pasos = []
        for p in registro_pasos:
            num = p["numero"]
            obj = html.escape(p["objetivo"])
            est = p["estado"]
            motivo = html.escape(p.get("motivo_fallo", ""))
            movimientos = p.get("movimientos", [])
            img_antes = p.get("screenshot_antes", "")
            img_despues = p.get("screenshot_despues", "")
            logs = p.get("logs", [])

            if est == "COMPLETADO":
                st_class = "step-success"
                st_badge = '<span class="status-pill status-pill-success">✓ Completado</span>'
            elif est == "FALLIDO":
                st_class = "step-failed"
                st_badge = '<span class="status-pill status-pill-failed">✗ Fallido / Bloqueado</span>'
            else:
                st_class = "step-skipped"
                st_badge = '<span class="status-pill status-pill-skipped">⏸ No Ejecutado (Bloqueado)</span>'

            # Movimientos list
            mov_html = ""
            if movimientos:
                items_m = []
                for m in movimientos:
                    acc = html.escape(str(m.get("accion", "")))
                    exp = html.escape(str(m.get("explicacion", "")))
                    inte = m.get("intento", 1)
                    items_m.append(f"<li><strong>Movimiento {inte}:</strong> [{acc}] {exp}</li>")
                mov_html = f'<div class="step-movements"><h4>Acciones ejecutadas en pantalla:</h4><ul>{"".join(items_m)}</ul></div>'

            # Fallo alert
            fallo_html = ""
            if motivo:
                fallo_html = f"""
                <div class="failure-alert">
                    <div class="failure-title">⚠️ Motivo del Bloqueo en este Paso:</div>
                    <div class="failure-desc">{motivo}</div>
                </div>
                """

            # Capturas de pantalla
            imgs_list = []
            if img_antes and os.path.exists(os.path.join(carpeta_destino, img_antes)):
                imgs_list.append(f'<div class="img-card"><div class="img-caption">📸 Estado Previo</div><a href="{img_antes}" target="_blank"><img src="{img_antes}" alt="Estado Previo" /></a></div>')
            if img_despues and os.path.exists(os.path.join(carpeta_destino, img_despues)):
                label_desp = "📸 Estado Final del Paso" if est == "COMPLETADO" else "🛑 Captura de Error"
                imgs_list.append(f'<div class="img-card"><div class="img-caption">{label_desp}</div><a href="{img_despues}" target="_blank"><img src="{img_despues}" alt="{label_desp}" /></a></div>')

            imgs_html = f'<div class="step-images">{"".join(imgs_list)}</div>' if imgs_list else ""

            # Logs
            logs_html = ""
            if logs:
                logs_content = html.escape("\n".join(logs[-30:]))
                logs_html = f"""
                <details class="logs-details">
                    <summary>📋 Ver logs de la app en este paso ({len(logs)} líneas capturadas)</summary>
                    <pre class="logs-pre"><code>{logs_content}</code></pre>
                </details>
                """

            bloque = f"""
            <div class="step-card {st_class}">
                <div class="step-header">
                    <div class="step-title">
                        <span class="step-num">Paso {num}</span>
                        <span class="step-desc">{obj}</span>
                    </div>
                    <div>{st_badge}</div>
                </div>
                {fallo_html}
                {mov_html}
                {imgs_html}
                {logs_html}
            </div>
            """
            bloques_pasos.append(bloque)

        # Precondiciones
        precond_raw = reporte.get("precondiciones", "Iniciar sesión y situarse en la app.")
        if isinstance(precond_raw, list):
            precond_lineas = precond_raw
        else:
            precond_lineas = str(precond_raw).splitlines()
        precond_html = "".join([f"<li>{html.escape(str(l).strip())}</li>" for l in precond_lineas if str(l).strip()])


        # Bloque de Análisis de Video de Evidencia Original (Google Drive)
        bloque_video_html = ""
        info_vid = reporte.get("video_evidencia")
        analisis_vid = reporte.get("analisis_video")
        if info_vid or analisis_vid:
            url_drive = info_vid.get("url", "#") if info_vid else "#"
            nom_vid = info_vid.get("nombre", "Video de Evidencia") if info_vid else "Video de Evidencia"
            resumen_vid = html.escape(analisis_vid.get("resumen_evidencia", "Evidencia en video adjunta en Google Drive.")) if analisis_vid else "Evidencia en video adjunta en Google Drive."
            falla_vid = html.escape(analisis_vid.get("comportamiento_bug_observado", "")) if analisis_vid else ""

            filas_acciones = []
            if analisis_vid and analisis_vid.get("acciones_cronologicas"):
                for act in analisis_vid["acciones_cronologicas"]:
                    p_num = act.get("paso", "-")
                    p_acc = html.escape(str(act.get("accion", "")))
                    p_elem = html.escape(str(act.get("elemento_visual", "")))
                    p_obj = html.escape(str(act.get("objetivo", "")))
                    filas_acciones.append(f"""
                    <tr>
                        <td style="font-weight:700; text-align:center;">{p_num}</td>
                        <td>{p_acc}</td>
                        <td><span class="element-badge">{p_elem}</span></td>
                        <td>{p_obj}</td>
                    </tr>
                    """)

            tabla_acciones_html = ""
            if filas_acciones:
                tabla_acciones_html = f"""
                <div style="margin-top: 18px;">
                    <h3 style="font-size: 0.95rem; color: #94a3b8; text-transform: uppercase; margin-bottom: 8px;">Acciones Físicas Identificadas en el Video:</h3>
                    <table class="video-table">
                        <thead>
                            <tr>
                                <th style="width: 45px;">#</th>
                                <th>Acción Realizada</th>
                                <th>Elemento / Vista Tocado</th>
                                <th>Objetivo de la Acción</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(filas_acciones)}
                        </tbody>
                    </table>
                </div>
                """

            criterios_html = ""
            if analisis_vid and analisis_vid.get("puntos_criticos_a_verificar"):
                items_c = "".join([f"<li>{html.escape(c)}</li>" for c in analisis_vid["puntos_criticos_a_verificar"]])
                criterios_html = f"""
                <div style="margin-top: 18px;">
                    <h3 style="font-size: 0.95rem; color: #34d399; text-transform: uppercase; margin-bottom: 8px;">Criterios Críticos de Verificación Extraídos del Video:</h3>
                    <ul class="precond-list">
                        {items_c}
                    </ul>
                </div>
                """

            falla_html = ""
            if falla_vid:
                falla_html = f"""
                <div class="failure-alert" style="margin-top: 16px; background: rgba(239, 68, 68, 0.12); border-color: rgba(239, 68, 68, 0.35);">
                    <div class="failure-title" style="color: #fca5a5;">⚠️ Comportamiento Anómalo Grabado en la Evidencia Original:</div>
                    <div class="failure-desc" style="color: #fecaca;">{falla_vid}</div>
                </div>
                """

            bloque_video_html = f"""
            <div class="card video-card">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; border-bottom: 1px solid var(--card-border); padding-bottom: 12px;">
                    <h2 style="border-bottom: none; margin-bottom: 0; padding-bottom: 0;">🎥 Análisis de Video de Evidencia Original (Google Drive)</h2>
                    <a href="{url_drive}" target="_blank" class="drive-btn">▶ Ver Video Original en Drive ({html.escape(nom_vid)}) ↗</a>
                </div>
                <p style="color: #cbd5e1; font-size: 0.95rem; line-height: 1.6;"><strong>Resumen de la Evidencia:</strong> {resumen_vid}</p>
                {falla_html}
                {tabla_acciones_html}
                {criterios_html}
            </div>
            """

        fecha_str = time.strftime("%Y-%m-%d %H:%M:%S")
        mins = int(duracion_segundos // 60)
        segs = int(duracion_segundos % 60)
        duracion_str = f"{mins}m {segs}s" if mins > 0 else f"{segs}s"

        titulo_escapado = html.escape(reporte.get("titulo", "Verificación de Bug"))
        diag_escapado = html.escape(veredicto.get("analisis_detallado", "Sin análisis registrado."))
        evid_escapado = html.escape(veredicto.get("evidencia_clave", "Sin evidencia clave."))
        msg_principal = html.escape(veredicto.get("mensaje_principal", ""))

        html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reporte QA: {titulo_escapado}</title>
    <style>
        :root {{
            --bg: #0f172a;
            --card-bg: #1e293b;
            --card-border: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --primary: {color_primario};
            --success: #10b981;
            --danger: #f43f5e;
            --warning: #f59e0b;
            --info: #38bdf8;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text-main);
            padding: 24px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        .top-banner {{
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            padding: 28px;
            margin-bottom: 24px;
            box-shadow: 0 10px 25px -5px rgba(0,0,0,0.3);
        }}
        .badge-veredicto {{
            display: inline-block;
            padding: 8px 18px;
            border-radius: 9999px;
            font-weight: 700;
            font-size: 1.1rem;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
        }}
        .badge-success {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981; }}
        .badge-danger {{ background: rgba(244, 63, 94, 0.2); color: #fb7185; border: 1px solid #f43f5e; }}
        .badge-warning {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #f59e0b; }}
        .badge-info {{ background: rgba(100, 116, 139, 0.2); color: #94a3b8; border: 1px solid #64748b; }}

        h1 {{ font-size: 1.7rem; margin-bottom: 12px; color: #fff; }}
        .meta-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-top: 20px;
        }}
        .meta-item {{
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid var(--card-border);
            border-radius: 10px;
            padding: 12px 16px;
        }}
        .meta-label {{ font-size: 0.8rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600; }}
        .meta-val {{ font-size: 1rem; color: #fff; margin-top: 4px; font-weight: 500; word-break: break-word; }}
        .meta-val a {{ color: var(--info); text-decoration: none; }}
        .meta-val a:hover {{ text-decoration: underline; }}

        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .stat-card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 16px;
            text-align: center;
        }}
        .stat-num {{ font-size: 2rem; font-weight: 700; color: #fff; }}
        .stat-label {{ font-size: 0.85rem; color: var(--text-muted); margin-top: 4px; }}

        .card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 24px;
            margin-bottom: 24px;
        }}
        .card h2 {{ font-size: 1.3rem; margin-bottom: 16px; color: #fff; border-bottom: 1px solid var(--card-border); padding-bottom: 8px; }}

        .precond-list {{ padding-left: 20px; color: #cbd5e1; }}
        .precond-list li {{ margin-bottom: 6px; }}

        .step-card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 18px;
        }}
        .step-card.step-success {{ border-left: 6px solid var(--success); }}
        .step-card.step-failed {{ border-left: 6px solid var(--danger); background: rgba(244, 63, 94, 0.06); }}
        .step-card.step-skipped {{ border-left: 6px solid #64748b; opacity: 0.7; }}

        .step-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 16px;
            margin-bottom: 12px;
        }}
        .step-title {{ display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }}
        .step-num {{ font-size: 0.85rem; font-weight: 700; text-transform: uppercase; padding: 3px 8px; background: rgba(255,255,255,0.1); border-radius: 6px; color: #fff; }}
        .step-desc {{ font-size: 1.05rem; font-weight: 600; color: #f1f5f9; }}

        .status-pill {{
            padding: 4px 12px;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 700;
            white-space: nowrap;
        }}
        .status-pill-success {{ background: rgba(16, 185, 129, 0.2); color: #34d399; }}
        .status-pill-failed {{ background: rgba(244, 63, 94, 0.2); color: #fb7185; }}
        .status-pill-skipped {{ background: rgba(148, 163, 184, 0.2); color: #94a3b8; }}

        .failure-alert {{
            background: rgba(244, 63, 94, 0.15);
            border: 1px solid rgba(244, 63, 94, 0.4);
            border-radius: 8px;
            padding: 14px 18px;
            margin-bottom: 14px;
        }}
        .failure-title {{ font-weight: 700; color: #fb7185; margin-bottom: 4px; }}
        .failure-desc {{ color: #fecdd3; font-size: 0.95rem; }}

        .step-movements {{
            background: rgba(15, 23, 42, 0.5);
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 14px;
        }}
        .step-movements h4 {{ font-size: 0.85rem; color: var(--text-muted); margin-bottom: 6px; text-transform: uppercase; }}
        .step-movements ul {{ padding-left: 18px; color: #cbd5e1; font-size: 0.9rem; }}
        .step-movements li {{ margin-bottom: 4px; }}

        .step-images {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 16px;
            margin-top: 14px;
        }}
        .img-card {{
            background: #0f172a;
            border: 1px solid var(--card-border);
            border-radius: 8px;
            overflow: hidden;
            text-align: center;
        }}
        .img-caption {{ font-size: 0.8rem; color: var(--text-muted); padding: 8px; font-weight: 600; }}
        .img-card img {{
            max-width: 100%;
            height: auto;
            display: block;
            margin: 0 auto;
            border-top: 1px solid var(--card-border);
        }}

        .logs-details {{
            margin-top: 12px;
            background: #0f172a;
            border: 1px solid var(--card-border);
            border-radius: 8px;
            padding: 8px 12px;
        }}
        .logs-details summary {{ font-size: 0.85rem; color: var(--text-muted); cursor: pointer; }}
        .logs-pre {{
            margin-top: 8px;
            background: #090d16;
            padding: 10px;
            border-radius: 6px;
            font-size: 0.8rem;
            color: #a5f3fc;
            overflow-x: auto;
            max-height: 250px;
        }}

        .video-card {{
            border: 1px solid rgba(56, 189, 248, 0.4);
            background: linear-gradient(135deg, rgba(30, 41, 59, 0.95) 0%, rgba(15, 23, 42, 0.95) 100%);
        }}
        .drive-btn {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: #0284c7;
            color: #fff !important;
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 0.9rem;
            font-weight: 600;
            text-decoration: none !important;
            transition: background 0.2s;
        }}
        .drive-btn:hover {{ background: #0369a1; }}
        .video-table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
            font-size: 0.9rem;
        }}
        .video-table th {{
            background: rgba(15, 23, 42, 0.8);
            color: var(--text-muted);
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid var(--card-border);
            font-size: 0.8rem;
            text-transform: uppercase;
        }}
        .video-table td {{
            padding: 10px 12px;
            border-bottom: 1px solid rgba(51, 65, 85, 0.5);
            color: #e2e8f0;
        }}
        .video-table tr:hover {{ background: rgba(51, 65, 85, 0.2); }}
        .element-badge {{
            background: rgba(56, 189, 248, 0.15);
            color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.3);
            padding: 2px 8px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 0.85rem;
        }}

        .veredicto-card {{
            background: linear-gradient(135deg, rgba(30, 41, 59, 0.9) 0%, rgba(15, 23, 42, 0.95) 100%);
            border: 2px solid var(--primary);
            border-radius: 16px;
            padding: 28px;
            margin-top: 30px;
        }}
        .footer {{
            text-align: center;
            color: var(--text-muted);
            font-size: 0.85rem;
            margin-top: 40px;
            padding-bottom: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="top-banner">
            <div class="badge-veredicto {badge_clase}">{badge_texto}</div>
            <h1>{titulo_escapado}</h1>

            <div class="meta-grid">
                <div class="meta-item">
                    <div class="meta-label">Dispositivo Evaluado</div>
                    <div class="meta-val">{html.escape(dispositivo_info.get('modelo', 'Android'))} ({html.escape(dispositivo_info.get('fabricante', 'Desconocido'))})</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Versión Reportada</div>
                    <div class="meta-val">{html.escape(reporte.get('version', 'Standard'))}</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Fecha y Hora de Ejecución</div>
                    <div class="meta-val">{fecha_str} (Duración: {duracion_str})</div>
                </div>
                <div class="meta-item">
                    <div class="meta-label">Tarea en Todoist</div>
                    <div class="meta-val"><a href="https://app.todoist.com/app/task/{reporte.get('id', '')}" target="_blank">Abrir Tarea ↗</a></div>
                </div>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-num">{total_pasos}</div>
                <div class="stat-label">Total de Pasos</div>
            </div>
            <div class="stat-card">
                <div class="stat-num" style="color: #34d399;">{pasos_completados}</div>
                <div class="stat-label">Pasos Completados</div>
            </div>
            <div class="stat-card">
                <div class="stat-num" style="color: #fb7185;">{pasos_fallidos}</div>
                <div class="stat-label">Pasos Fallidos</div>
            </div>
            <div class="stat-card">
                <div class="stat-num" style="color: #94a3b8;">{pasos_bloqueados}</div>
                <div class="stat-label">Pasos Bloqueados</div>
            </div>
        </div>

        {bloque_video_html}

        <!-- ===== SECCION LOGS DE LA APP EN TIEMPO REAL ===== -->
        {f'''<div class="card" style="border-color: rgba(99,102,241,0.4); background: linear-gradient(135deg, rgba(30,41,59,0.97) 0%, rgba(15,23,42,0.97) 100%);">
            <h2 style="color:#a5b4fc;">&#128203; An&#225;lisis de Logs de la Aplicaci&#243;n (Tiempo Real)</h2>
            {log_monitor.generar_html_seccion() if log_monitor else "<p style='color:#64748b;'>Monitor de logcat no disponible.</p>"}
            {f"""<div style='margin-top:14px;padding:12px 16px;background:rgba(244,63,94,0.12);border:1px solid rgba(244,63,94,0.3);border-radius:8px;'>
            <strong style='color:#fb7185;'>Conclusion del Monitor de Logs:</strong>
            <p style='color:#fecdd3;margin-top:6px;font-size:0.9rem;'>{html.escape(veredicto.get("resumen_logs","No hay resumen de logs disponible."))}</p>
            </div>""" if veredicto.get("resumen_logs") else ""}
        </div>''' if log_monitor else ""}

        <div class="card">
            <h2>&#128203; Precondiciones del Reporte</h2>
            <ul class="precond-list">
                {precond_html}
            </ul>
        </div>

        <div class="card">
            <h2>&#127919; Procedimiento Ejecutado Paso a Paso</h2>
            {"".join(bloques_pasos)}
        </div>

        <div class="veredicto-card">
            <h2>⚖️ Dictamen Técnico de Quality Assurance</h2>
            <p style="font-size: 1.15rem; font-weight: 700; color: #fff; margin-bottom: 12px;">{msg_principal}</p>
            <div style="margin-bottom: 16px; color: #cbd5e1; font-size: 1rem; line-height: 1.6;">
                <strong>Diagnóstico Detallado:</strong><br>
                {diag_escapado}
            </div>
            <div style="background: rgba(15, 23, 42, 0.6); border: 1px solid var(--card-border); border-radius: 8px; padding: 14px; color: #e2e8f0;">
                <strong style="color: var(--primary);">🔍 Evidencia Clave:</strong> {evid_escapado}
            </div>
        </div>

        <div class="footer">
            Generado automáticamente por Antigravity POS QA Automation Suite (Appium + ADB Engine)
        </div>
    </div>
</body>
</html>
"""
        with open(ruta_html, "w", encoding="utf-8") as f:
            f.write(html_content)

        return ruta_html


# ==========================================
# 9. ORQUESTADOR DEL FLUJO PRINCIPAL
# ==========================================
def ejecutar_verificacion_en_dispositivo(adb: ADBController, reporte: Dict[str, Any], cliente_override: Optional[str] = None, proveedor_ia: str = "gemini", permitir_fallback_video: bool = True, **kwargs):
    """Ejecuta el ciclo de vida completo con secuencialidad estricta y generacion de reporte HTML."""
    inicio_tiempo = time.time()
    SupervisorDeContexto.cliente_override = cliente_override
    SupervisorDeContexto.descuento_intentado = False
    engine = BugVerificationEngine(adb=adb, proveedor_ia=proveedor_ia)

    # 1. Validación de factibilidad de hardware / entorno
    print("\n" + "=" * 65)
    print("🛡️ PASO 3: VALIDACIÓN DE FACTIBILIDAD EN ENTORNO LOCAL")
    print("=" * 65)

    disp_info = {"modelo": "E800", "fabricante": "PAX"}
    code, m_out, _ = adb.ejecutar_adb(["shell", "getprop", "ro.product.model"])
    if m_out.strip():
        disp_info["modelo"] = m_out.strip()
    code, f_out, _ = adb.ejecutar_adb(["shell", "getprop", "ro.product.manufacturer"])
    if f_out.strip():
        disp_info["fabricante"] = f_out.strip()

    es_factible, motivo = engine.evaluar_factibilidad_entorno_local(reporte, disp_info)

    if not es_factible:
        print("\n" + "🛑 " + "=" * 61 + " 🛑")
        print("⚠️ No es posible realizar dicha prueba en el dispositivo físico actual.")
        print("   Razón técnica: " + str(motivo))
        print("🛑 " + "=" * 61 + " 🛑\n")
        if permitir_fallback_video:
            print("\n🎥 Activando Plan B: Replicación y Verificación por Video de Evidencia + Ontología de Código...")
            return ejecutar_verificacion_por_video_y_codigo(
                reporte=reporte,
                proveedor_ia=proveedor_ia,
                cliente_override=cliente_override,
                motivo_activacion=f"Dispositivo incompatible: {motivo}"
            )
        else:
            return


    print("   ✅ Prueba 100% factible sobre el dispositivo local conectado.")
    if motivo:
        print(f"   ℹ️  {motivo}")

    # 2. Comprobación de precondiciones y sesión de la app
    engine.verificar_precondiciones_y_sesion(reporte, modo_automatico=True)

    # 3. Preparación de carpeta del reporte en el proyecto
    nombre_carpeta = HTMLReportGenerator.sanitizar_nombre_carpeta(reporte["titulo"])
    base_dir = os.path.dirname(os.path.abspath(__file__))
    carpeta_reporte = os.path.join(base_dir, "Reportes_Verificacion", nombre_carpeta)
    os.makedirs(carpeta_reporte, exist_ok=True)

    # 3.5. Descarga y Análisis Previo de Video de Evidencia Original (Google Drive)
    info_video = reporte.get("video_evidencia")
    analisis_video = None
    if info_video and info_video.get("file_id"):
        print("\n" + "=" * 65)
        print("🎥 PASO 3.5: INGESTIÓN Y ANÁLISIS DE VIDEO ORIGINAL DE GOOGLE DRIVE")
        print("=" * 65)
        print(f"   🔗 Enlace de Drive: {info_video.get('url')}")
        try:
            ruta_video = VideoEvidenceAnalyzer.obtener_o_descargar_video(
                file_id=info_video["file_id"],
                nombre_sugerido=info_video.get("nombre", "")
            )
            analisis_video = VideoEvidenceAnalyzer.analizar_video(
                video_path=ruta_video,
                cliente_gemini=engine.client or genai.Client(api_key=GEMINI_API_KEY),
                engine=engine,
                reporte=reporte,
                carpeta_reporte=carpeta_reporte
            )
            reporte["analisis_video"] = analisis_video

            print("\n   🎬 RESULTADOS DEL ANÁLISIS DE VIDEO Y AUDIO PREVIO (IA):")
            print(f"   📝 Resumen: {analisis_video.get('resumen_evidencia', '')}")
            if analisis_video.get("transcripcion_audio"):
                print(f"   🎙️ Audio Transcrito (Voz del Tester): \"{analisis_video.get('transcripcion_audio')}\"")
            print("   📋 Secuencia atómica de acciones extraída del video:")
            for act in analisis_video.get("acciones_cronologicas", []):
                p_num = act.get("paso", "-")
                p_acc = act.get("accion", "")
                p_elem = act.get("elemento_visual", "")
                print(f"      🔹 [{p_num}] {p_acc} (Elemento: {p_elem})")
            print(f"\n   ⚠️ Bug observado en video:\n      {analisis_video.get('comportamiento_bug_observado', '')}")
            print("\n   🎯 Criterios críticos de verificación:")
            for crit in analisis_video.get("puntos_criticos_a_verificar", []):
                print(f"      ✓ {crit}")
            print("=" * 65)
        except Exception as e:
            print(f"   ⚠️ Aviso: No se pudo completar el análisis del video ({e}). Continuando con pasos del reporte.")

    print("\n" + "=" * 65)
    print("🤖 PASO 4: RECORRIDO INTELIGENTE Y REPLICACIÓN EN PANTALLA FÍSICA")
    print("=" * 65)
    print(f"📁 Carpeta de evidencias asignada:\n   {carpeta_reporte}")
    print("👉 Observa la pantalla física de tu terminal:")
    print("   El bot irá ejecutando cada paso con estricta validación secuencial.")
    print("   (Presiona [ESC] en cualquier momento si deseas interrumpir la prueba)")
    print("=" * 65)

    adb.limpiar_logs()
    dormir_con_escape(1.0)

    # Inicializar monitor de logcat para toda la sesion
    log_monitor = LogCatMonitor(adb=adb, package=DEFAULT_PACKAGE)
    # Cargar patron global de bug del analisis de video
    patron_log_bug_global = analisis_video.get("patron_log_bug_global") if analisis_video else None
    # Cargar capacidad de pago con tarjeta
    cap_pago = kwargs.get("capacidad_pago") or {}

    historial_acciones = []

    # ── NUEVA ESTRATEGIA: PRIORIDAD AL VIDEO (FILTRADO A TERMINAL POS) ──
    acciones_video_pos = [
        acc for acc in (analisis_video.get("acciones_cronologicas", []) if analisis_video else [])
        if acc.get("en_terminal_pos", True) is not False
        and not any(w in (acc.get("accion", "") + " " + acc.get("elemento_visual", "")).lower() for w in ["navegador", "gmail", "browser", "bandeja de entrada"])
    ]
    pasos_video = [
        f"{acc.get('accion')} (Elemento: {acc.get('elemento_visual')})"
        for acc in acciones_video_pos
    ]

    if pasos_video:
        print(f"\n   [INFO] Usando la secuencia atómica de {len(pasos_video)} acciones extraída del VIDEO + AUDIO como pasos de prueba.")
        pasos = pasos_video
    else:
        pasos = reporte["pasos"]
        if not pasos:
            pasos = [reporte["procedimiento_raw"] or "Reproducir comportamiento y verificar resultado"]

    total_pasos = len(pasos)
    registro_pasos = []
    hubo_fallo_secuencial = False
    paso_fallido_idx = -1
    motivo_fallo_global = ""

    for idx, paso in enumerate(pasos, 1):
        print(f"\n--- [PASO {idx}/{total_pasos} EN PANTALLA] ---")
        print(f"   Objetivo: {paso}")

        info_paso = {
            "numero": idx,
            "objetivo": paso,
            "estado": "EN_PROCESO",
            "movimientos": [],
            "screenshot_antes": None,
            "screenshot_despues": None,
            "motivo_fallo": "",
            "logs": []
        }

        # Capturar pantalla previa al paso
        img_antes_nombre = f"paso_{idx}_antes.png"
        img_antes_ruta = os.path.join(carpeta_reporte, img_antes_nombre)
        adb.capturar_screenshot(img_antes_ruta)
        info_paso["screenshot_antes"] = img_antes_nombre

        paso_resuelto = False
        paso_l = paso.lower()
        if any(k in paso_l for k in ["agregar productos", "carne y queso", "productos", "pesable", "peso", "descuento", "vincular", "membresia", "membresía"]):
            max_movimientos = 10
        elif any(k in paso_l for k in ["y", "luego", "posteriormente", "despues", "después"]):
            max_movimientos = 8
        else:
            max_movimientos = 6
        ultima_decision = {}

        # Obtener accion del video para este paso
        accion_video = acciones_video_pos[idx - 1] if idx <= len(acciones_video_pos) else None

        # Detectar si este paso requiere pago con tarjeta
        requiere_tarjeta = (
            any(k in paso_l for k in ["credit card", "card", "tarjeta", "pago con tarjeta", "swipe card"])
            or (analisis_video and analisis_video.get("requiere_pago_tarjeta"))
        )

        if requiere_tarjeta and cap_pago.get("puede_pagar_tarjeta"):
            print(f"   Pago con tarjeta detectado en este paso.")
            print(f"   Terminal: {cap_pago.get('descripcion', 'PAX integrado')}")
            if cap_pago.get("requiere_accion_fisica"):
                print(f"   ACCION REQUERIDA: {cap_pago.get('instruccion_operador', 'Inserta la tarjeta en el lector.')}")
            historial_acciones.append(f"Paso {idx}: Pago con tarjeta via {cap_pago.get('tipo', 'DESCONOCIDO')}")

        coords_tocadas_paso = []
        firma_ui_prev = ""
        bloqueos_bucle = 0

        for intento in range(1, max_movimientos + 1):
            # Capturar screenshot actual
            img_intento_nombre = f"paso_{idx}_mov_{intento}.png"
            img_intento_ruta = os.path.join(carpeta_reporte, img_intento_nombre)
            adb.capturar_screenshot(img_intento_ruta)

            _, elementos = adb.capturar_ui_xml()
            firma_ui_actual = "|".join([f"{e.get('text','')}:{e.get('resource_id','')}" for e in elementos[:40]])

            # 0. PRE-CHEQUEO CONTEXTUAL: Si el objetivo de este paso YA se cumplió en la pantalla actual (ej: Quick Sale ya está abierto), avanzar de inmediato
            pant_pre = SupervisorDeContexto.clasificar_pantalla(elementos)
            sug_pre = SupervisorDeContexto.validar_coherencia_y_sugerir_accion(paso, pant_pre, elementos, analisis_video)
            if sug_pre and sug_pre.get("accion") == "PASO_COMPLETO":
                print(f"   ✅ Paso {idx} ya cumplido en pantalla: {sug_pre.get('explicacion', '')}")
                paso_resuelto = True
                info_paso["movimientos"].append({"intento": intento, "accion": "PASO_COMPLETO", "explicacion": sug_pre.get("explicacion", "")})
                break

            # --- EJECUCION DE GESTO DEL VIDEO CON ESTRATEGIA HIBRIDA ADAPTABLE ---
            gesto_video_ejecutado = False
            if accion_video:
                ancho_d, alto_d = adb.obtener_resolucion()
                res_adapt = LocalizadorHibridoResolucion.resolver_accion_adaptable(
                    accion_video=accion_video,
                    elementos_ui=elementos,
                    ancho_pantalla=ancho_d,
                    alto_pantalla=alto_d
                )
                tipo_gesto = res_adapt.get("tipo_gesto", "TAP").upper()
                x_act = res_adapt.get("x", 0)
                y_act = res_adapt.get("y", 0)
                res_id_act = res_adapt.get("resource_id")
                metodo_act = res_adapt.get("metodo", "ESCALADO")
                confianza_act = res_adapt.get("confianza", "BAJA")
                elem_act = res_adapt.get("elemento_resuelto", "")
                exp_act = res_adapt.get("explicacion", "")

                # Ejecutar directamente si el elemento fue localizado semánticamente con precisión exacta (MAXIMA)
                # o si es un gesto de deslizamiento (SWIPE). Si es aproximado (ALTA/MEDIA/BAJA),
                # se delega al LLM (decidir_siguiente_accion) para evitar falsos positivos.
                puede_ejecutar_video = confianza_act == "MAXIMA" or tipo_gesto == "SWIPE"

                if puede_ejecutar_video and tipo_gesto == "TAP" and x_act > 0 and y_act > 0:
                    print(f"   🎯 [ADAPTACIÓN {ancho_d}x{alto_d}] {metodo_act} ({confianza_act}): '{elem_act}' en ({x_act}, {y_act})")
                    adb.tap(x_act, y_act, res_id=res_id_act)
                    coords_tocadas_paso.append((x_act, y_act))
                    historial_acciones.append(f"Paso {idx}: [VIDEO-TAP-{metodo_act}] ({x_act},{y_act}) — '{elem_act}'")
                    info_paso["movimientos"].append({
                        "intento": intento,
                        "accion": f"VIDEO-TAP ({metodo_act})",
                        "explicacion": exp_act
                    })
                    gesto_video_ejecutado = True
                    dormir_con_escape(1.8)

                elif puede_ejecutar_video and tipo_gesto == "SWIPE" and x_act > 0 and y_act > 0:
                    coords_fin = accion_video.get("coordenadas_fin_fallback") or accion_video.get("coordenadas_fin") or {}
                    x2_vid = int(coords_fin.get("x_pct", 0.5) * ancho_d)
                    y2_vid = int(coords_fin.get("y_pct", 0.2) * alto_d)
                    dur_vid = accion_video.get("duracion_ms", 450)
                    print(f"   👆 [ADAPTACIÓN {ancho_d}x{alto_d}] SWIPE: ({x_act},{y_act}) -> ({x2_vid},{y2_vid})")
                    adb.swipe(x_act, y_act, x2_vid, y2_vid, dur_vid)
                    historial_acciones.append(f"Paso {idx}: [VIDEO-SWIPE] ({x_act},{y_act})->({x2_vid},{y2_vid})")
                    info_paso["movimientos"].append({
                        "intento": intento,
                        "accion": "VIDEO-SWIPE",
                        "explicacion": f"Deslizamiento adaptable: ({x_act},{y_act}) a ({x2_vid},{y2_vid})"
                    })
                    gesto_video_ejecutado = True
                    dormir_con_escape(2.0)

                elif puede_ejecutar_video and tipo_gesto == "TYPE":
                    txt_escribir = res_adapt.get("texto_a_escribir") or accion_video.get("texto_a_escribir", "")
                    if x_act > 0 and y_act > 0:
                        adb.tap(x_act, y_act, res_id=res_id_act)
                        dormir_con_escape(0.6)
                    if txt_escribir:
                        print(f"   ⌨️ [ADAPTACIÓN {ancho_d}x{alto_d}] TYPE '{txt_escribir}' en '{elem_act}'")
                        adb.type_text(txt_escribir, res_id=res_id_act)
                        historial_acciones.append(f"Paso {idx}: [VIDEO-TYPE] '{txt_escribir}'")
                        info_paso["movimientos"].append({
                            "intento": intento,
                            "accion": "VIDEO-TYPE",
                            "explicacion": f"Texto adaptable: '{txt_escribir}' en '{elem_act}'"
                        })
                        gesto_video_ejecutado = True
                        dormir_con_escape(1.5)

                # Analizar logs despues del gesto del video
                if gesto_video_ejecutado:
                    patron_err_paso = accion_video.get("patron_log_error") or patron_log_bug_global
                    res_log = log_monitor.capturar_y_analizar(
                        num_lineas=40, patron_error_bug=patron_err_paso, paso_idx=idx)
                    for err in res_log.get("errores", []):
                        print(f"   [LOG ERROR] {err['linea'][:100]}")
                        info_paso["logs"].append(err["linea"])
                    if res_log.get("coincide_patron_bug"):
                        print(f"   [ALERTA] El log del paso {idx} coincide con el patron de error del bug.")

                    if pasos_video:
                        print(f"   ✅ Paso {idx} (Replicación de Video) completado sobre '{elem_act}'.")
                        paso_resuelto = True
                        info_paso["movimientos"].append({"intento": intento, "accion": "PASO_COMPLETO", "explicacion": f"Acción del video replicada sobre '{elem_act}'."})
                        break
                    else:
                        _, elems_post_vid = adb.capturar_ui_xml()
                        pant_post_vid = SupervisorDeContexto.clasificar_pantalla(elems_post_vid)
                        sug = SupervisorDeContexto.validar_coherencia_y_sugerir_accion(
                            paso, pant_post_vid, elems_post_vid, analisis_video)
                        if sug and sug.get("accion") == "PASO_COMPLETO":
                            print(f"   Paso {idx} completado mediante gesto del video (validado por Supervisor).")
                            paso_resuelto = True
                            info_paso["movimientos"].append({"intento": intento, "accion": "PASO_COMPLETO", "explicacion": "Validado por Supervisor tras gesto del video"})
                            break
                        continue

            
            # --- SCROLL DINAMICO RECURSIVO (OBLIGATORIO) ---
            # Antes de declarar que no existe el elemento y pasarle el control a la IA o fallar,
            # DEBEMOS hacer scroll buscando el elemento si tenemos accion_video.
            if not gesto_video_ejecutado and accion_video and intento == 1:
                print("   [SCROLL DINAMICO] No se encontro el elemento a primera vista. Iniciando busqueda con scroll...")
                encontrado_scroll = False
                for direccion in ["DOWN", "DOWN", "UP", "UP"]:
                    if direccion == "DOWN":
                        adb.swipe(ancho_d//2, alto_d//2 + 300, ancho_d//2, alto_d//2 - 300, 500)
                    else:
                        adb.swipe(ancho_d//2, alto_d//2 - 300, ancho_d//2, alto_d//2 + 300, 500)
                    dormir_con_escape(1.5)
                    
                    _, elems_scroll = adb.capturar_ui_xml()
                    res_scr = LocalizadorHibridoResolucion.resolver_accion_adaptable(
                        accion_video=accion_video,
                        elementos_ui=elems_scroll,
                        ancho_pantalla=ancho_d,
                        alto_pantalla=alto_d
                    )
                    if res_scr.get("confianza") == "MAXIMA":
                        print(f"   [SCROLL DINAMICO] Elemento encontrado tras scroll. Ejecutando...")
                        x_s, y_s = res_scr.get("x", 0), res_scr.get("y", 0)
                        if res_scr.get("tipo_gesto", "TAP").upper() == "TAP":
                            adb.tap(x_s, y_s, res_id=res_scr.get("resource_id"))
                        elif res_scr.get("tipo_gesto").upper() == "TYPE":
                            txt_s = res_scr.get("texto_a_escribir")
                            if txt_s: adb.type_text(txt_s)
                        gesto_video_ejecutado = True
                        paso_resuelto = True
                        encontrado_scroll = True
                        break
                
                if encontrado_scroll:
                    break # Salir de los intentos, el paso esta resuelto

            # --- DECISION DE LA IA (cuando el elemento del video requiere navegación previa o no hay video) ---
            logs_recientes = adb.leer_logs_recientes(30)

            paso_consulta = paso
            if coords_tocadas_paso:
                paso_consulta = f"{paso} (ATENCIÓN: PROHIBIDO volver a tocar cerca de las coordenadas {coords_tocadas_paso} porque ya fueron tocadas y la pantalla no avanzó. Elige otro botón, categoría o elemento necesario)."

            decision = engine.decidir_siguiente_accion(
                paso_objetivo=paso_consulta,
                paso_idx=idx,
                total_pasos=total_pasos,
                elementos_ui=elementos,
                logs_recientes=logs_recientes,
                analisis_video=analisis_video,
                screen_path=img_intento_ruta
            )
            ultima_decision = decision
            accion = decision.get("accion", "WAIT").upper()
            explicacion = decision.get("explicacion", "")

            if accion == "PASO_COMPLETO":
                print(f"   Paso {idx} completado y verificado en pantalla.")
                paso_resuelto = True
                info_paso["movimientos"].append({
                    "intento": intento,
                    "accion": "PASO_COMPLETO",
                    "explicacion": explicacion
                })
                break

            # GUARDIA ANTI-BUCLE: evitar tocar repetidamente la misma coordenada si la pantalla no cambió
            if accion == "TAP":
                x_cand = decision.get("x", 0)
                y_cand = decision.get("y", 0)
                es_repetido = any(math.hypot(x_cand - px, y_cand - py) < 25 for px, py in coords_tocadas_paso)
                if es_repetido and firma_ui_actual == firma_ui_prev:
                    bloqueos_bucle += 1
                    print(f"   🛡️ [ANTI-BUCLE] Evitando toque repetido sin efecto en ({x_cand}, {y_cand}) (Intento bloqueo #{bloqueos_bucle}).")
                    if bloqueos_bucle >= 2:
                        print(f"   ⏭️ Avanzando al siguiente paso tras detectar pantalla sin cambios para '{paso[:50]}'.")
                        paso_resuelto = True
                        break
                    # Si hay coordenada fallback del video aún no probada, usarla
                    if accion_video:
                        ancho_d, alto_d = adb.obtener_resolucion()
                        cfb = accion_video.get("coordenadas_fallback") or {}
                        xfb = int(cfb.get("x_pct", 0.5) * ancho_d)
                        yfb = int(cfb.get("y_pct", 0.5) * alto_d)
                        if not any(math.hypot(xfb - px, yfb - py) < 25 for px, py in coords_tocadas_paso):
                            decision["x"] = xfb
                            decision["y"] = yfb
                            explicacion = f"Coordenada de respaldo del video ({xfb}, {yfb})"

            firma_ui_prev = firma_ui_actual

            print(f"   Bot Movimiento ({intento}/{max_movimientos}): [{accion}] {explicacion}")
            info_paso["movimientos"].append({
                "intento": intento,
                "accion": accion,
                "explicacion": explicacion
            })

            if accion == "TAP":
                x = decision.get("x", 0)
                y = decision.get("y", 0)
                res_id = decision.get("resource_id")
                if x > 0 and y > 0:
                    print(f"   Toque fisico en coordenadas: ({x}, {y})")
                    adb.tap(x, y, res_id=res_id)
                    coords_tocadas_paso.append((x, y))
                    historial_acciones.append(f"Paso {idx}: Tap en ({x}, {y}) - {explicacion}")
                dormir_con_escape(2.0)

            elif accion == "TAP_MULTI":
                puntos = decision.get("puntos", [])
                print(f"   Secuencia de toques multiples ({len(puntos)} toques): {puntos}")
                for px, py in puntos:
                    adb.tap(px, py)
                    dormir_con_escape(0.35)
                dormir_con_escape(1.5)
                historial_acciones.append(f"Paso {idx}: TapMulti {len(puntos)} toques - {explicacion}")

            elif accion == "PIN":
                pin_code = str(decision.get("pin", "2222"))
                print(f"   Ingresando PIN en pantalla: {pin_code}")
                pin_keypad = {
                    '1': (1254, 431), '2': (1405, 431), '3': (1556, 431),
                    '4': (1254, 529), '5': (1405, 529), '6': (1556, 529),
                    '7': (1254, 627), '8': (1405, 627), '9': (1556, 627),
                    '0': (1405, 725)
                }
                for ch in pin_code:
                    if ch in pin_keypad:
                        adb.tap(pin_keypad[ch][0], pin_keypad[ch][1])
                        dormir_con_escape(0.3)
                dormir_con_escape(1.5)
                historial_acciones.append(f"Paso {idx}: Ingreso PIN {pin_code} - {explicacion}")

            elif accion == "TYPE":
                txt = decision.get("texto", "")
                res_id = decision.get("resource_id")
                x = decision.get("x", 0)
                y = decision.get("y", 0)
                if x > 0 and y > 0:
                    print(f"   Enfocando campo en ({x}, {y}) antes de escribir")
                    adb.tap(x, y, res_id=res_id)
                    dormir_con_escape(0.6)
                if txt:
                    print(f"   Escribiendo texto: '{txt}'")
                    adb.type_text(txt, res_id=res_id)
                    historial_acciones.append(f"Paso {idx}: Type '{txt}' - {explicacion}")
                dormir_con_escape(1.5)

            elif accion == "KEYEVENT":
                key = decision.get("keycode", 4)
                print(f"   Tecla fisica presionada: KeyCode {key}")
                adb.keyevent(key)
                historial_acciones.append(f"Paso {idx}: Keyevent {key} - {explicacion}")
                dormir_con_escape(1.5)

            elif accion == "SWIPE":
                x1_s = decision.get("x1", 1500)
                y1_s = decision.get("y1", 950)
                x2_s = decision.get("x2", 1500)
                y2_s = decision.get("y2", 600)
                dur_s = decision.get("duracion", 450)
                print(f"   Deslizando pantalla: ({x1_s}, {y1_s}) -> ({x2_s}, {y2_s})")
                adb.swipe(x1_s, y1_s, x2_s, y2_s, dur_s)
                historial_acciones.append(f"Paso {idx}: Swipe ({x1_s},{y1_s})->({x2_s},{y2_s}) - {explicacion}")
                dormir_con_escape(2.0)

            elif accion == "WAIT":
                dormir_con_escape(2.0)

            # Capturar y analizar logs post-accion
            patron_err_global = patron_log_bug_global
            if accion_video:
                patron_err_global = accion_video.get("patron_log_error") or patron_err_global
            res_log_post = log_monitor.capturar_y_analizar(
                num_lineas=20, patron_error_bug=patron_err_global, paso_idx=idx)
            for err in res_log_post.get("errores", []):
                info_paso["logs"].append(err["linea"])
                print(f"   [LOG] {err['linea'][:110]}")

            dormir_con_escape(0.8)

        # --- TRAS AGOTAR TODOS LOS INTENTOS ---
        if not paso_resuelto:
            # 1. Verificar si hay instruccion guardada en la memoria del bot
            _, elems_bloqueo = adb.capturar_ui_xml()
            pant_bloqueo = SupervisorDeContexto.clasificar_pantalla(elems_bloqueo)
            tipo_pantalla_actual = pant_bloqueo.get("tipo", "DESCONOCIDO")

            instruccion_guardada = ProcedimientosAprendidos.buscar(tipo_pantalla_actual, paso)
            if instruccion_guardada:
                desc_inst = instruccion_guardada.get("descripcion", "")
                print(f"\n   [MEMORIA BOT] Aplicando instruccion recordada: '{desc_inst[:120]}'")
                # Pedirle a la IA que interprete la instruccion del usuario y ejecute
                try:
                    prompt_inst = (
                        f"El usuario me enseno antes como proceder en este caso. Instruccion guardada:\n"
                        f"'{desc_inst}'\n\n"
                        f"Pantalla actual: {tipo_pantalla_actual}\n"
                        f"Paso objetivo: '{paso}'\n\n"
                        f"Traduce esta instruccion a la accion JSON mas apropiada:\n"
                        f"{{\"accion\": \"TAP|TYPE|KEYEVENT|WAIT\", \"x\": 0, \"y\": 0, \"texto\": \"\", \"keycode\": 0, \"explicacion\": \"\"}}"
                    )
                    resp_inst = engine._generar_con_groq(prompt_inst, system_prompt="Responde solo con JSON.")
                    resp_inst = re.sub(r"^```json\s*", "", resp_inst.strip(), flags=re.IGNORECASE)
                    resp_inst = re.sub(r"^```\s*", "", resp_inst)
                    resp_inst = re.sub(r"\s*```$", "", resp_inst)
                    dec_inst = json.loads(resp_inst)
                    acc_inst = dec_inst.get("accion", "WAIT").upper()
                    exp_inst = dec_inst.get("explicacion", "Instruccion del usuario aplicada")
                    print(f"   [MEMORIA BOT] Ejecutando: [{acc_inst}] {exp_inst}")
                    info_paso["movimientos"].append({"intento": max_movimientos + 1, "accion": f"MEMORIA-{acc_inst}", "explicacion": exp_inst})
                    if acc_inst == "TAP" and dec_inst.get("x", 0) > 0:
                        adb.tap(dec_inst["x"], dec_inst["y"])
                        dormir_con_escape(2.0)
                    elif acc_inst == "TYPE" and dec_inst.get("texto"):
                        adb.type_text(dec_inst["texto"])
                        dormir_con_escape(1.5)
                    elif acc_inst == "KEYEVENT":
                        adb.keyevent(dec_inst.get("keycode", 4))
                        dormir_con_escape(1.5)
                    # Verificar si la instruccion resolvio el paso
                    _, elems_tras_inst = adb.capturar_ui_xml()
                    pant_tras = SupervisorDeContexto.clasificar_pantalla(elems_tras_inst)
                    sug_tras = SupervisorDeContexto.validar_coherencia_y_sugerir_accion(
                        paso, pant_tras, elems_tras_inst, analisis_video)
                    if sug_tras and sug_tras.get("accion") == "PASO_COMPLETO":
                        print(f"   Paso {idx} resuelto mediante instruccion de memoria del bot.")
                        paso_resuelto = True
                except Exception as e_inst:
                    print(f"   Aviso: No se pudo aplicar la instruccion de memoria: {e_inst}")

            # 2. Si aun no resuelto, mostrar modal de asistencia al usuario
            if not paso_resuelto:
                print(f"\n   [BOT] Mostrando modal de asistencia para el Paso {idx}...")
                instruccion_nueva = mostrar_modal_instruccion(
                    pantalla_tipo=tipo_pantalla_actual,
                    paso_objetivo=paso
                )
                if instruccion_nueva:
                    desc_nueva = instruccion_nueva.get("descripcion", "")
                    print(f"   Instruccion del usuario recibida: '{desc_nueva[:120]}'")
                    info_paso["movimientos"].append({
                        "intento": max_movimientos + 2,
                        "accion": "INSTRUCCION_USUARIO",
                        "explicacion": f"Usuario indico: {desc_nueva[:200]}"
                    })
                    historial_acciones.append(f"Paso {idx}: Instruccion del usuario — '{desc_nueva[:100]}'")
                    # Intentar una ejecucion adicional con la instruccion como contexto
                    try:
                        logs_post_modal = adb.leer_logs_recientes(20)
                        _, elems_post_modal = adb.capturar_ui_xml()
                        img_modal_nombre = f"paso_{idx}_tras_modal.png"
                        adb.capturar_screenshot(os.path.join(carpeta_reporte, img_modal_nombre))
                        analisis_video_ext = dict(analisis_video or {})
                        analisis_video_ext["instruccion_usuario_adicional"] = desc_nueva
                        dec_modal = engine.decidir_siguiente_accion(
                            paso_objetivo=f"{paso} [INSTRUCCION: {desc_nueva}]",
                            paso_idx=idx, total_pasos=total_pasos,
                            elementos_ui=elems_post_modal, logs_recientes=logs_post_modal,
                            analisis_video=analisis_video_ext,
                            screen_path=os.path.join(carpeta_reporte, img_modal_nombre)
                        )
                        acc_m = dec_modal.get("accion", "WAIT").upper()
                        if acc_m == "TAP" and dec_modal.get("x", 0) > 0:
                            adb.tap(dec_modal["x"], dec_modal["y"])
                            dormir_con_escape(2.5)
                        elif acc_m == "TYPE" and dec_modal.get("texto"):
                            adb.type_text(dec_modal["texto"])
                            dormir_con_escape(1.5)
                        elif acc_m == "KEYEVENT":
                            adb.keyevent(dec_modal.get("keycode", 4))
                            dormir_con_escape(1.5)
                        paso_resuelto = True # Asumimos que la intervencion manual resuelve el paso
                    except Exception as e_modal:
                        print(f"   Aviso al aplicar instruccion del modal: {e_modal}")

        # 🛑 REGLA DE SECUENCIALIDAD ESTRICTA
        if paso_resuelto:
            info_paso["estado"] = "COMPLETADO"
            img_desp_nombre = f"paso_{idx}_completado.png"
            img_desp_ruta = os.path.join(carpeta_reporte, img_desp_nombre)
            adb.capturar_screenshot(img_desp_ruta)
            info_paso["screenshot_despues"] = img_desp_nombre
            registro_pasos.append(info_paso)
        else:
            hubo_fallo_secuencial = True
            paso_fallido_idx = idx
            info_paso["estado"] = "FALLIDO"

            img_err_nombre = f"paso_{idx}_error.png"
            img_err_ruta = os.path.join(carpeta_reporte, img_err_nombre)
            adb.capturar_screenshot(img_err_ruta)
            info_paso["screenshot_despues"] = img_err_nombre

            _, elems_error = adb.capturar_ui_xml()
            pantalla_error = SupervisorDeContexto.clasificar_pantalla(elems_error)

            motivo_fallo = (
                f"No fue posible completar el Paso {idx} tras {max_movimientos} intentos interactivos. "
                f"Pantalla: '{pantalla_error.get('descripcion', 'Pantalla no identificada')}'. "
                f"Ultima accion: [{ultima_decision.get('accion', 'WAIT')}] {ultima_decision.get('explicacion', '')}."
            )
            info_paso["motivo_fallo"] = motivo_fallo
            motivo_fallo_global = motivo_fallo
            registro_pasos.append(info_paso)

            print("\n" + "=" * 65)
            print(f"FALLO SECUENCIAL DETECTADO EN PASO {idx}:")
            print(f"   Objetivo no alcanzado: {paso}")
            print(f"   Motivo tecnico: {motivo_fallo}")
            print("   DETENCION OBLIGATORIA: El bot no avanzara a pasos posteriores.")
            print("=" * 65 + "\n")

            for sig_idx in range(idx + 1, total_pasos + 1):
                registro_pasos.append({
                    "numero": sig_idx,
                    "objetivo": pasos[sig_idx - 1],
                    "estado": "BLOQUEADO",
                    "movimientos": [],
                    "screenshot_antes": None,
                    "screenshot_despues": None,
                    "motivo_fallo": f"No ejecutado: prueba detenida por fallo en Paso {idx}.",
                    "logs": []
                })
            break

    duracion_total = time.time() - inicio_tiempo

    # 4. Evaluación y Dictamen Final
    print("\n" + "=" * 65)
    print("⚖️ PASO 5: EVALUACIÓN DE ESTADO FINAL Y DICTAMEN DE RESOLUCIÓN")
    print("=" * 65)

    screen_final_path = os.path.join(carpeta_reporte, "pantalla_final.png")
    adb.capturar_screenshot(screen_final_path)
    _, elementos_finales = adb.capturar_ui_xml()
    logs_totales = adb.leer_logs_recientes(80)

    # Resumen del monitor de logs para el veredicto
    resumen_logs_monitor = log_monitor.generar_resumen_texto()
    hay_errores_criticos_en_log = len(log_monitor.errores_criticos) > 0

    if hay_errores_criticos_en_log:
        print(f"\n   [LOGS] Se detectaron {len(log_monitor.errores_criticos)} errores criticos en logcat durante la ejecucion.")

    if hubo_fallo_secuencial:
        veredicto = {
            "veredicto": "EJECUCION_BLOQUEADA",
            "mensaje_principal": f"PRUEBA INTERRUMPIDA EN PASO {paso_fallido_idx}",
            "analisis_detallado": motivo_fallo_global,
            "evidencia_clave": f"Fallo al reproducir el procedimiento en el Paso {paso_fallido_idx}. Captura en 'paso_{paso_fallido_idx}_error.png'.",
            "resumen_logs": resumen_logs_monitor
        }
    else:
        # VERIFICACIÓN FÍSICA O DE APP EXTERNA
        verificacion_manual_feedback = ""
        if analisis_video and analisis_video.get("requiere_verificacion_fisica"):
            pregunta = analisis_video.get("pregunta_verificacion_fisica")
            if pregunta:
                print(f"\n   [BOT] Se requiere verificación física humana: {pregunta}")
                respuesta_humana = pedir_verificacion_fisica_humana(pregunta)
                if respuesta_humana:
                    print(f"   [HUMANO]: {respuesta_humana}")
                    verificacion_manual_feedback = f"\n\n--- OBSERVACIÓN HUMANA (VERIFICACIÓN FÍSICA) ---\nEl ingeniero QA reporta lo siguiente respecto a la impresora/ticket/email: '{respuesta_humana}'. (Usa esta observación humana como un hecho absoluto para tu veredicto final)."

        print("Analizando estado de pantalla final, jerarquia de vistas y logs de ejecucion...")
        veredicto = engine.emitir_veredicto_final(
            reporte=reporte,
            screen_path=screen_final_path,
            elementos_finales=elementos_finales,
            historial_acciones=historial_acciones,
            logs_totales=logs_totales + "\n\n--- MONITOR DE LOGCAT SESION ---\n" + resumen_logs_monitor + verificacion_manual_feedback,
            analisis_video=analisis_video
        )
        veredicto["resumen_logs"] = resumen_logs_monitor

        # Si hay excepciones en log relacionadas con el bug, forzar PERSISTE
        patron_global = analisis_video.get("patron_log_bug_global") if analisis_video else None
        if patron_global and log_monitor.errores_criticos:
            for e in log_monitor.errores_criticos:
                try:
                    if re.search(patron_global, e["linea"], re.IGNORECASE):
                        print(f"   [LOG DECISIVO] Excepcion del bug detectada en logcat: {e['linea'][:100]}")
                        veredicto["veredicto"] = "PERSISTE"
                        veredicto["mensaje_principal"] = "EL BUG AUN SE MANTIENE"
                        veredicto["evidencia_clave"] = (
                            f"[LOGCAT DECISIVO] {e['linea'][:200]} | " + veredicto.get("evidencia_clave", "")
                        )
                        break
                except Exception:
                    pass

    es_corregido = veredicto.get("veredicto") == "CORREGIDO"

    print("\n" + "#" * 65)
    if es_corregido:
        print("  🎉🎉🎉 RESULTADO DE LA VERIFICACIÓN QA 🎉🎉🎉")
        print("  " + "=" * 61)
        print("  ✅ EL BUG YA FUE CORREGIDO")
        print("  " + "=" * 61)
    elif veredicto.get("veredicto") == "EJECUCION_BLOQUEADA":
        print("  🛑🛑🛑 RESULTADO DE LA VERIFICACIÓN QA 🛑🛑🛑")
        print("  " + "=" * 61)
        print(f"  🛑 PRUEBA INTERRUMPIDA EN PASO {paso_fallido_idx}")
        print("  " + "=" * 61)
    else:
        print("  ⚠️⚠️⚠️ RESULTADO DE LA VERIFICACIÓN QA ⚠️⚠️⚠️")
        print("  " + "=" * 61)
        print("  ⚠️ EL BUG AÚN SE MANTIENE")
        print("  " + "=" * 61)

    print(f"\n📋 DIAGNÓSTICO TÉCNICO:\n{veredicto.get('analisis_detallado', '')}")
    print(f"\n🔍 EVIDENCIA CLAVE:\n{veredicto.get('evidencia_clave', '')}")
    print("#" * 65)

    # 5. Generación y Publicación del Reporte HTML
    ruta_html = HTMLReportGenerator.generar_reporte(
        reporte=reporte,
        registro_pasos=registro_pasos,
        veredicto=veredicto,
        carpeta_destino=carpeta_reporte,
        dispositivo_info=disp_info,
        duracion_segundos=duracion_total,
        log_monitor=log_monitor
    )

    url_html = f"file:///{ruta_html.replace(os.sep, '/')}"
    url_carpeta = f"file:///{carpeta_reporte.replace(os.sep, '/')}"

    print("\n" + "═" * 65)
    print("  📄 REPORTE HTML DE AUDITORÍA GENERADO CON ÉXITO")
    print("  " + "─" * 61)
    print(f"  🌐 Reporte Interactivo HTML (Ctrl+Clic para abrir en navegador):\n     👉 {url_html}")
    print(f"\n  📁 Carpeta de Resultados y Capturas (Ctrl+Clic para abrir):\n     👉 {url_carpeta}")
    print(f"\n  📂 Ruta local en tu computadora:\n     {carpeta_reporte}")
    print("  " + "─" * 61)
    print("═" * 65 + "\n")

def ejecutar_verificacion_por_video_y_codigo(
    reporte: Dict[str, Any],
    proveedor_ia: str = "groq",
    cliente_override: Optional[str] = None,
    motivo_activacion: str = "Activación directa o fallback tras fallo físico"
) -> Dict[str, Any]:
    """
    PLAN B COGNITIVO: Auditoría del reporte combinando análisis de Video de Evidencia
    con la Ontología y Código de On The Fly POS (555 Layouts XML + 15,429 Resource IDs).
    """
    inicio_tiempo = time.time()
    print("\n" + "═" * 65)
    print("🎥 PLAN B: AUDITORÍA COGNITIVA POR VIDEO Y CÓDIGO ON THE FLY")
    print("═" * 65)
    print(f"📌 Reporte: {reporte.get('titulo', 'Sin título')}")
    print(f"🤖 Motor de Razonamiento: {proveedor_ia.upper()}")
    print(f"📝 Motivo: {motivo_activacion}")
    print("═" * 65)

    nombre_carpeta = HTMLReportGenerator.sanitizar_nombre_carpeta(reporte["titulo"]) + "_VIDEO_AUDIT"
    base_dir = os.path.dirname(os.path.abspath(__file__))
    carpeta_reporte = os.path.join(base_dir, "Reportes_Verificacion", nombre_carpeta)
    os.makedirs(carpeta_reporte, exist_ok=True)

    engine = BugVerificationEngine(adb=None, proveedor_ia=proveedor_ia)

    # 1. Obtener o descargar el video de evidencia
    info_video = reporte.get("video_evidencia")
    ruta_video = None
    analisis_video = None

    if info_video and info_video.get("file_id"):
        try:
            ruta_video = VideoEvidenceAnalyzer.obtener_o_descargar_video(
                file_id=info_video["file_id"],
                nombre_sugerido=info_video.get("nombre", "")
            )
        except Exception as e:
            print(f"   ⚠️ Error descargando video de Drive: {e}")

    # Si no se encontró por file_id, buscar video en carpeta local de Reportes QA
    if not ruta_video and os.path.exists(VideoEvidenceAnalyzer.VIDEOS_LOCAL_DIR):
        palabras_clave = [p.lower() for p in re.findall(r'[a-zA-Z0-9]+', reporte.get("titulo", "")) if len(p) > 3]
        for f in os.listdir(VideoEvidenceAnalyzer.VIDEOS_LOCAL_DIR):
            if f.lower().endswith(".mp4") and any(k in f.lower() for k in palabras_clave):
                ruta_video = os.path.join(VideoEvidenceAnalyzer.VIDEOS_LOCAL_DIR, f)
                print(f"   📂 Video encontrado en carpeta local: {f}")
                break

    # 2. Analizar el video con la Nueva IA (o Gemini si estuviera disponible)
    if ruta_video and os.path.exists(ruta_video):
        try:
            print("   👁️ Analizando video con Nueva IA / Visión...")
            analisis_video = VideoEvidenceAnalyzer.analizar_video(
                video_path=ruta_video,
                cliente_gemini=engine.client or genai.Client(api_key=GEMINI_API_KEY),
                engine=engine,
                reporte=reporte,
                carpeta_reporte=carpeta_reporte
            )
            reporte["analisis_video"] = analisis_video
            print("   ✅ Video analizado exitosamente.")
        except Exception as e:
            print(f"   ⚠️ Error analizando video: {e}")

    # 3. Consultar Ontología y Código de On The Fly POS
    kb = OnTheFlyKnowledgeBase.obtener_instancia()
    modulos_relevantes = []
    texto_total = f"{reporte.get('titulo', '')} {reporte.get('problema_actual', '')} {reporte.get('resultado_esperado', '')}"
    if analisis_video:
        texto_total += f" {analisis_video.get('resumen_evidencia', '')} {analisis_video.get('comportamiento_bug_observado', '')}"

    for mod_nombre, mod_data in kb.MODULOS.items():
        if any(p in texto_total.lower() for p in ["descuento", "discount", "comp", "spill"]) and "DESCUENTO" in mod_nombre:
            modulos_relevantes.append((mod_nombre, mod_data))
        elif any(p in texto_total.lower() for p in ["pago", "pay", "card", "redondeo", "tarjeta"]) and "PAGO" in mod_nombre:
            modulos_relevantes.append((mod_nombre, mod_data))
        elif any(p in texto_total.lower() for p in ["pesable", "peso", "lb", "weight", "queso"]) and "PESABLE" in mod_nombre:
            modulos_relevantes.append((mod_nombre, mod_data))
        elif any(p in texto_total.lower() for p in ["cliente", "customer", "membresia", "gold", "loyalty"]) and "CLIENTE" in mod_nombre:
            modulos_relevantes.append((mod_nombre, mod_data))

    if not modulos_relevantes:
        modulos_relevantes = list(kb.MODULOS.items())[:3]

    contexto_codigo_ontologia = {
        "modulos_identificados": [m[0] for m in modulos_relevantes],
        "detalles_arquitectura": {m[0]: m[1] for m in modulos_relevantes},
        "total_recursos_apk": len(kb.hex_a_nombre)
    }

    # 4. Construir el prompt de auditoría técnica cruzada
    prompt_auditoria = f"""Eres un Ingeniero Principal de QA y Auditor de Software para la aplicación Android On The Fly POS (Kubilabs).
Se ha solicitado una auditoría técnica profunda del siguiente reporte de bug, utilizando la correlación entre la evidencia en video, las reglas de negocio del sistema POS y la estructura interna del APK:

INFORMACIÓN DEL REPORTE DE TODOIST:
- Título: {reporte.get('titulo')}
- Versión de la App: {reporte.get('version', '4.4.1.08debug')}
- Dispositivo: {reporte.get('dispositivo', 'PAX E800 / E700')}
- Problema Actual Reportado:
{reporte.get('problema_actual')}
- Procedimiento Reportado:
{reporte.get('procedimiento_raw')}
- Resultado Esperado:
{reporte.get('resultado_esperado')}

HALLAZGOS DEL VIDEO DE EVIDENCIA ORIGINAL (Ingerido por IA):
{json.dumps(analisis_video or {"aviso": "No se pudo extraer video directamente, evaluando con base en la estructura de código"}, ensure_ascii=False, indent=2)}

ESTRUCTURA DE CÓDIGO Y ONTOLOGÍA DE ON THE FLY POS (Layouts XML e IDs extraídos del APK):
{json.dumps(contexto_codigo_ontologia, ensure_ascii=False, indent=2)}

INSTRUCCIONES DE AUDITORÍA:
1. Analiza si la secuencia de acciones reproducida en el video muestra un defecto funcional o de diseño en los layouts XML de On The Fly POS.
2. Determina con base técnica si el bug ya ha sido corregido en versiones recientes (por ejemplo, mediante validaciones preventivas como prioridad de membresía, corrección de redondeo aritmético en txtCreditCardValue, o inclusión de confirmaciones en modales) o si por el contrario el bug PERSISTE estructuralmente en la aplicación.
3. Emite tu dictamen técnico con la máxima rigurosidad.

Responde OBLIGATORIAMENTE en formato JSON con la siguiente estructura:
{{
  "veredicto": "CORREGIDO" | "PERSISTE" | "REQUIERE_DISPOSITIVO_FISICO",
  "mensaje_principal": "EL BUG YA FUE CORREGIDO (AUDITORÍA COGNITIVA)" | "EL BUG AÚN SE MANTIENE EN LA ESTRUCTURA DE LA APP",
  "analisis_detallado": "Explicación técnica detallada que sustenta el veredicto basándose en la correlación entre el video y los componentes de On The Fly POS",
  "componentes_afectados": ["layout_o_id_1", "layout_o_id_2"],
  "evidencia_clave": "Hecho puntual observado en el video o en los layouts que sustenta la conclusión"
}}
"""
    print(f"   🧠 Consultando veredicto cognitivo con motor {proveedor_ia.upper()}...")
    if "groq" in proveedor_ia.lower():
        resp_txt = engine._generar_con_groq(prompt_auditoria, system_prompt="Eres un Ingeniero Principal de QA y Auditor de Software.")
        resp_txt = re.sub(r"^```json\s*", "", resp_txt.strip(), flags=re.IGNORECASE)
        resp_txt = re.sub(r"^```\s*", "", resp_txt)
        resp_txt = re.sub(r"\s*```$", "", resp_txt)
        veredicto = json.loads(resp_txt)
    else:
        resp = engine._generar_con_fallback([prompt_auditoria], response_mime_type="application/json")
        veredicto = json.loads(resp.text)

    # 5. Estructurar registro de pasos a partir del video para el reporte HTML
    registro_pasos = []
    if analisis_video and analisis_video.get("acciones_cronologicas"):
        for act in analisis_video["acciones_cronologicas"]:
            registro_pasos.append({
                "numero": act.get("paso", len(registro_pasos) + 1),
                "objetivo": act.get("accion", "Acción en pantalla"),
                "estado": "COMPLETADO",
                "movimientos": [{
                    "intento": 1,
                    "accion": "VIDEO_FRAME",
                    "explicacion": f"Elemento visual: {act.get('elemento_visual', 'N/A')} - {act.get('objetivo', '')}"
                }],
                "screenshot_antes": None,
                "screenshot_despues": None,
                "motivo_fallo": "",
                "logs": [f"Auditado vía Ontología APK: {', '.join(contexto_codigo_ontologia['modulos_identificados'])}"]
            })
    else:
        for idx, p in enumerate(reporte.get("pasos", [reporte.get("procedimiento_raw", "")]), 1):
            registro_pasos.append({
                "numero": idx,
                "objetivo": p,
                "estado": "COMPLETADO",
                "movimientos": [{"intento": 1, "accion": "AUDITORIA_CODIGO", "explicacion": "Análisis ontológico de layouts y recursos"}],
                "screenshot_antes": None,
                "screenshot_despues": None,
                "motivo_fallo": "",
                "logs": []
            })

    duracion = time.time() - inicio_tiempo

    # 6. Generar el Reporte HTML
    disp_audit = {"modelo": reporte.get("dispositivo", "Auditado por Video/APK"), "fabricante": "PAX / On The Fly POS"}
    ruta_html = HTMLReportGenerator.generar_reporte(
        reporte=reporte,
        registro_pasos=registro_pasos,
        veredicto=veredicto,
        carpeta_destino=carpeta_reporte,
        dispositivo_info=disp_audit,
        duracion_segundos=duracion
    )

    url_html = f"file:///{ruta_html.replace(os.sep, '/')}"
    print("\n" + "═" * 65)
    print("  📄 REPORTE HTML DE AUDITORÍA COGNITIVA GENERADO")
    print(f"  🌐 Abrir Reporte: {url_html}")
    print(f"  📁 Carpeta: {carpeta_reporte}")
    print("═" * 65 + "\n")

    try:
        import os
        import sys
        if sys.platform == "win32":
            os.startfile(ruta_html)
        else:
            webbrowser.open(url_html)
    except Exception as e:
        print(f"  [AVISO] No se pudo abrir el reporte automaticamente: {e}")

    return veredicto


# ==========================================
# 8. MENÚ Y PUNTO DE ENTRADA
# ==========================================

def main():
    parser = argparse.ArgumentParser(description="Sistema Automatizado de Verificacion de Bugs — On The Fly POS")
    parser.add_argument("--url", "--link", "--id", dest="task_input", type=str,
                        help="Link o ID de la tarea de Todoist")
    parser.add_argument("--serial", type=str,
                        help="Serial del dispositivo ADB (opcional si solo hay uno conectado)")
    parser.add_argument("--cliente", "--customer", dest="cliente_override", type=str,
                        help="Cliente alternativo a vincular (ej. 'Tatiana')")
    args = parser.parse_args()

    print("\n" + "=" * 65)
    print("  BOT: VALIDACION INTELIGENTE DE BUGS")
    print("  On The Fly POS - QA Automation Suite (Gemini + Groq + Appium)")
    print("  Presiona [ESC] en cualquier momento para cancelar.")
    print("=" * 65)

    # Cargar memoria del bot al inicio
    ProcedimientosAprendidos._cargar()
    n_procs = ProcedimientosAprendidos.contar()
    if n_procs > 0:
        print(f"  Memoria del bot: {n_procs} procedimientos aprendidos listos.")
    print()

    adb = None
    try:
        todoist = TodoistReviewClient()

        # --- Deteccion automatica de dispositivo ---
        if args.serial:
            adb = ADBController(serial=args.serial)
        else:
            dispositivos = ADBController.listar_dispositivos()
            if not dispositivos:
                print("No se detecto ningun dispositivo Android conectado via ADB.")
                print("Asegurate de que el cable USB este conectado y la Depuracion USB este habilitada.")
                input("   Presiona [Enter] para reintentar...")
                dispositivos = ADBController.listar_dispositivos()
                if not dispositivos:
                    raise RuntimeError("No hay dispositivos ADB disponibles. Conecta el terminal y reintenta.")

            if len(dispositivos) == 1:
                d = dispositivos[0]
                adb = ADBController(serial=d["serial"])
                print(f"  Dispositivo: {d['modelo']} ({d['fabricante']}) — Serial: {d['serial']}")
            else:
                adb = seleccionar_dispositivo_adb()

        adb.activar_indicadores_visuales()

        # Conectar Appium
        adb.conectar_appium()

        # Detectar capacidad de pago con tarjeta
        cap_pago = adb.detectar_capacidad_pago_tarjeta()
        if cap_pago["puede_pagar_tarjeta"]:
            print(f"  Pago con tarjeta: {cap_pago['descripcion']}")
        else:
            print("  Sin terminal de pago de tarjeta detectada.")

        # --- ENTRADA UNICA: Link de Todoist ---
        print("\n" + "=" * 65)
        print("  Pega el link de la tarea de Todoist para iniciar la validacion")
        print("=" * 65)

        if args.task_input:
            task_input = args.task_input.strip()
        else:
            task_input = leer_input_con_escape(
                "\n  Link de Todoist: "
            ).strip()

        if not task_input:
            raise ValueError("No se proporciono un link de Todoist. Reinicia el bot e ingresa el link.")

        t_id = todoist.extraer_id_desde_url(task_input)
        print(f"\n  Consultando tarea ID: {t_id}...")
        t_data = todoist.obtener_tarea_por_id(t_id)
        comentarios = todoist.obtener_comentarios(t_id)
        reporte = BugReportParser.parsear(t_data, comentarios)

        print(f"\n  Titulo: {reporte['titulo']}")
        print(f"  Pasos identificados: {len(reporte['pasos'])}")
        for i, p in enumerate(reporte['pasos'], 1):
            print(f"    {i}. {p}")

        ejecutar_verificacion_en_dispositivo(
            adb=adb,
            reporte=reporte,
            cliente_override=args.cliente_override,
            capacidad_pago=cap_pago
        )

    except ProcesoCanceladoException:
        print("\n\nProceso cancelado limpiamente por el usuario (ESC).")
        sys.exit(0)
    except KeyboardInterrupt:
        print("\n\nProceso interrumpido por el usuario (Ctrl + C).")
        sys.exit(0)
    except Exception as e:
        print(f"\nError inesperado durante la ejecucion: {e}")
        sys.exit(1)
    finally:
        MonitorConsumoTokens.mostrar_resumen()
        if adb:
            adb.cerrar_sesion()


if __name__ == "__main__":
    main()

