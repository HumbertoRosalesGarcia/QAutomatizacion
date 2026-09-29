"""
GUI Moderna y Simétrica para Automatización de Reportes de QA
Soporta Drag & Drop nativo de archivos (videos/imágenes),
atajos con tecla ESC para cancelar/revertir, y monitoreo en tiempo real.
"""

import os
import sys
import re
import threading
import time
import json
import webbrowser
import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import List, Optional

try:
    import windnd
    HAS_WINDND = True
except ImportError:
    HAS_WINDND = False

import subir_reporte_qa as core
from gemini_cuotas import gestor_cuotas
from gemini_keys_pool import gestor_pool, enmascarar_clave
from proveedor_alternativo import (
    MODELOS_GROQ_DISPONIBLES,
    cargar_configuracion_ia,
    guardar_configuracion_ia
)

ARCHIVO_VERSIONES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "versiones_app.json")


def cargar_versiones_app() -> List[str]:
    """Carga la lista de versiones guardadas desde disco o retorna la lista inicial."""
    if os.path.isfile(ARCHIVO_VERSIONES):
        try:
            with open(ARCHIVO_VERSIONES, "r", encoding="utf-8") as f:
                items = json.load(f)
                if isinstance(items, list) and items:
                    return items
        except Exception:
            pass
    versiones_defecto = ["4.4.1.09debug", "4.4.1.08debug", "4.4.1.10debug", "4.4.1.07", "4.4.0.0"]
    guardar_versiones_app(versiones_defecto)
    return versiones_defecto


def guardar_versiones_app(versiones: List[str]):
    """Persiste la lista de versiones en disco."""
    try:
        with open(ARCHIVO_VERSIONES, "w", encoding="utf-8") as f:
            json.dump(versiones, f, indent=2, ensure_ascii=False)
    except Exception:
        pass



# Paleta de colores Dark Modern (Slate / Indigo)
BG_MAIN = "#0f172a"        # Slate 900
BG_CARD = "#1e293b"        # Slate 800
BG_CARD_LIGHT = "#334155"  # Slate 700
BG_INPUT = "#0b0f19"       # Deep Dark
BORDER_COLOR = "#475569"   # Slate 600
TEXT_MAIN = "#f8fafc"      # Slate 50
TEXT_MUTED = "#94a3b8"     # Slate 400
ACCENT_BLUE = "#3b82f6"    # Blue 500
ACCENT_BLUE_HOVER = "#2563eb"
ACCENT_RED = "#ef4444"     # Red 500
ACCENT_RED_HOVER = "#dc2626"
ACCENT_GREEN = "#10b981"   # Emerald 500
ACCENT_YELLOW = "#f59e0b"  # Amber 500

FONT_TITLE = ("Segoe UI", 14, "bold")
FONT_SUBTITLE = ("Segoe UI", 9)
FONT_SECTION = ("Segoe UI", 10, "bold")
FONT_LABEL = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_MONO = ("Consolas", 9)


class RedirectOutput:
    """Redirige stdout y stderr simultáneamente a la consola y al widget visual."""
    def __init__(self, text_widget, root_widget, original_stream):
        self.text_widget = text_widget
        self.root_widget = root_widget
        self.original_stream = original_stream

    def write(self, string):
        if self.original_stream:
            try:
                self.original_stream.write(string)
                self.original_stream.flush()
            except UnicodeEncodeError:
                try:
                    encoding = getattr(self.original_stream, 'encoding', 'ascii') or 'ascii'
                    safe_str = string.encode(encoding, errors='replace').decode(encoding)
                    self.original_stream.write(safe_str)
                    self.original_stream.flush()
                except Exception:
                    pass
            except Exception:
                pass
        def _append():
            try:
                if self.text_widget.winfo_exists():
                    self.text_widget.configure(state="normal")
                    if string.startswith("\r"):
                        contenido = string.lstrip("\r")
                        self.text_widget.delete("end-1c linestart", "end-1c")
                        self.text_widget.insert("end-1c", contenido)
                    else:
                        self.text_widget.insert(tk.END, string)
                    self.text_widget.see(tk.END)
                    self.text_widget.configure(state="disabled")
            except Exception:
                pass
        try:
            self.root_widget.after(0, _append)
        except Exception:
            pass

    def flush(self):
        if self.original_stream:
            try:
                self.original_stream.flush()
            except Exception:
                pass


OPCIONES_MODELOS_GEMINI = {
    "3.6 Flash (Recomendado - Análisis de Video Nativo - 20 consultas/día gratis)": "gemini-3.6-flash",
    "3.8 Flash (Máxima Calidad y Análisis Profundo - 20 consultas/día gratis)": "gemini-3.8-flash"
}


def traducir_error_a_humano(error) -> dict:
    """Traduce errores crudos de API/red a explicaciones humanas, amables y accionables en español."""
    error_str = str(error).lower()

    if "gemini 3.6 flash no pudo analizar el video" in error_str or "fallo visión gemini 3.6" in error_str:
        return {
            "titulo": "Redacción Cancelada: Video No Analizado por Gemini 3.6",
            "mensaje": (
                "Al utilizar Groq para la redacción, el bot requiere obligatoriamente que Gemini 3.6 Flash "
                "inspeccione y extraiga la bitácora visual del video primero.\n\n"
                "⚠️ Motivo de la cancelación:\n"
                "Gemini 3.6 Flash no pudo completar la observación visual del video (por congestión temporal en Google o falta de respuesta).\n\n"
                "💡 Para evitar redactar un reporte incorrecto o sin análisis visual real, el proceso ha sido cancelado automáticamente.\n\n"
                "Por favor, espera unos segundos y vuelve a pulsar 'Generar y Subir Reporte'."
            )
        }
    elif "gemini-3.1-pro" in error_str and ("429" in error_str or "resource_exhausted" in error_str or "limit: 0" in error_str):
        return {
            "titulo": "El Motor 3.1 Pro Requiere Facturación en la API",
            "mensaje": (
                "Has seleccionado el motor '3.1 Pro', pero en el servicio de Google este modelo requiere facturación activa (límite 0).\n\n"
                "💡 ¿Cómo solucionarlo de inmediato?\n"
                "Selecciona '3.6 Flash (Recomendado - 1,500 consultas/día gratis)'. Analizará tu video nativamente con la máxima fidelidad."
            )
        }
    elif "429" in error_str or "resource_exhausted" in error_str or "rate limit" in error_str:
        return {
            "titulo": "Límite Temporal de Consultas Alcanzado",
            "mensaje": (
                "El motor de IA ha alcanzado momentáneamente el límite de peticiones por minuto de Google.\n\n"
                "💡 ¿Cómo continuar?\n"
                "• Espera unos 15 a 30 segundos y vuelve a pulsar 'Generar y Subir Reporte'.\n"
                "• Con Gemini 3.6 Flash dispones de 1,500 consultas diarias gratuitas."
            )
        }
    elif any(term in error_str for term in ["503", "unavailable", "high demand", "overloaded"]):
        return {
            "titulo": "Servidores de Google Ocupados Momentáneamente",
            "mensaje": (
                "Los servidores de Google Gemini experimentaron un pico temporal de demanda.\n\n"
                "💡 ¿Cómo solucionarlo?\n"
                "Espera unos segundos y pulsa nuevamente 'Generar y Subir'."
            )
        }
    elif any(term in error_str for term in ["connection", "timeout", "getaddrinfo", "timed out"]):
        return {
            "titulo": "Fallo de Conexión a Internet",
            "mensaje": (
                "No fue posible comunicarse con los servidores de Google o Todoist.\n\n"
                "💡 Por favor, verifica que tu equipo tenga conexión a Internet activa e inténtalo nuevamente."
            )
        }
    elif "invalid_grant" in error_str or "expired or revoked" in error_str:
        return {
            "titulo": "Sesión de Google Drive Expirada",
            "mensaje": (
                "El permiso de acceso a tu Google Drive ha expirado.\n\n"
                "💡 Pulsa 'Generar y Subir' nuevamente; se abrirá automáticamente tu navegador para autorizar tu cuenta de Google."
            )
        }
    elif "drive" in error_str:
        return {
            "titulo": "No se Pudo Subir a Google Drive",
            "mensaje": (
                "Ocurrió un problema al alojar el archivo de evidencia en Google Drive.\n\n"
                "💡 Verifica que el archivo de video o imagen no esté abierto en otra aplicación (como un reproductor o editor) y vuelve a intentar."
            )
        }
    elif "max_items_limit_reached" in error_str or "maximum number of items" in error_str:
        return {
            "titulo": "Límite de Tareas en Proyecto de Todoist (300/300)",
            "mensaje": (
                "El proyecto 'OTF Development' ha alcanzado el límite máximo de 300 tareas activas permitido por Todoist.\n\n"
                "💡 Tu reporte se guardó automáticamente en tu Bandeja de Entrada (Inbox) para no perderlo.\n"
                "• Puedes marcar como completadas las tareas antiguas en 'Pending deploy' para liberar espacio en el proyecto."
            )
        }
    elif "no se ha configurado la api key de groq" in error_str:
        return {
            "titulo": "Clave de API de Groq Requerida",
            "mensaje": (
                "Para utilizar el motor de Groq necesitas ingresar tu API Key gratuita.\n\n"
                "💡 Puedes obtenerla en console.groq.com/keys o cambiar al motor Google Gemini arriba."
            )
        }
    elif any(term in error_str for term in ["invalid_api_key", "invalid api key", "unauthorized", "authentication"]):
        return {
            "titulo": "Clave de API no Válida",
            "mensaje": (
                "La clave de API ingresada no es válida o ha sido revocada."
            )
        }
    elif "todoist" in error_str:
        return {
            "titulo": "Inconveniente con Todoist",
            "mensaje": (
                "No se pudo crear la tarea en tu cuenta de Todoist.\n\n"
                "💡 Verifica que el servicio de Todoist esté disponible y que la conexión a internet sea estable."
            )
        }
    else:
        return {
            "titulo": "Aviso en el Proceso",
            "mensaje": (
                f"El reporte no pudo completarse debido al siguiente motivo:\n\n{str(error)[:220]}\n\n"
                "💡 Sugerencia: Verifica la conexión e inténtalo nuevamente."
            )
        }


ESTADO_CUOTA_MODELOS = {
    "gemini-3.5-flash": {
        "badge": "🟢 Motor Recomendado (1,500 consultas/día gratis - Visión Nativa de Video)",
        "porcentaje": 1.0,
        "color": ACCENT_GREEN,
        "detalle": "Motor multimodal nativo de Google con 1,500 consultas diarias gratuitas. Analiza cuadro a cuadro el video, detecta toques y redacta el reporte exacto de tu Gem."
    },
    "gemini-3.8-flash": {
        "badge": "🟡 Motor Avanzado (Límite Google: 20 consultas/día)",
        "porcentaje": 1.0,
        "color": ACCENT_YELLOW,
        "detalle": "Motor con pensamiento extendido. Límite diario estricto de 20 consultas en el plan gratuito de Google AI Studio."
    },
    "gemini-3.1-pro-preview": {
        "badge": "🟣 Máxima Calidad Pro (Requiere facturación activa)",
        "porcentaje": 0.0,
        "color": "#a855f7",
        "detalle": "Motor Pro de Google. Límite 0 en cuentas gratuitas (requiere facturación Pay-as-you-go en Google Cloud)."
    },
    "gemini-3.6-flash": {
        "badge": "🟡 Modelo Intermedio (Equilibrado - 20 req/día)",
        "porcentaje": 0.50,
        "color": ACCENT_YELLOW,
        "detalle": "Modelo equilibrado de Google con límite diario de 20 peticiones."
    },
    "gemini-3.5-flash-lite": {
        "badge": "⚪ Motor Ligero Alternativo (1,500 req/día)",
        "porcentaje": 0.30,
        "color": TEXT_MUTED,
        "detalle": "Modelo ligero de alta velocidad con 1,500 consultas diarias gratuitas."
    }
}


class DialogoExitoWhatsApp(tk.Toplevel):
    """Ventana modal moderna que presenta el mensaje formateado para WhatsApp y enlaces de acceso directo."""
    def __init__(self, parent, resultado: dict, on_revertir=None):
        super().__init__(parent)
        self.resultado = resultado
        self.on_revertir = on_revertir
        self.title("🎉 Reporte Creado con Éxito - QA Automation")
        self.geometry("740x560")
        self.minsize(640, 480)
        self.configure(bg=BG_MAIN)
        self.transient(parent)
        self.grab_set()

        try:
            self.update_idletasks()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            x = max(0, px + (pw - 740) // 2)
            y = max(0, py + (ph - 560) // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

        # Asegurar formato exacto y limpio para WhatsApp
        from subir_reporte_qa import formatear_mensaje_whatsapp
        titulo_res = resultado.get("titulo", "")
        task_url = resultado.get("task_url", "")
        drive_link = resultado.get("drive_link", "")
        es_imagen = resultado.get("es_imagen", False)
        version_app = resultado.get("version_app", "4.4.1.08debug")

        if task_url and drive_link:
            self.mensaje_wa = formatear_mensaje_whatsapp(
                titulo=titulo_res,
                task_url=task_url,
                drive_link=drive_link,
                es_imagen=es_imagen,
                version_app=version_app
            )
        else:
            self.mensaje_wa = resultado.get("mensaje_whatsapp", "")

        resultado["mensaje_whatsapp"] = self.mensaje_wa
        mensaje_wa = self.mensaje_wa

        # Copiar automáticamente al portapapeles de Windows nada más abrir
        try:
            self.clipboard_clear()
            self.clipboard_append(self.mensaje_wa)
        except Exception:
            pass

        padre = tk.Frame(self, bg=BG_MAIN, padx=22, pady=18)
        padre.pack(fill=tk.BOTH, expand=True)

        # Header
        lbl_icono = tk.Label(padre, text="🎉  ¡Reporte Publicado con Éxito!", font=FONT_TITLE, fg=ACCENT_GREEN, bg=BG_MAIN)
        lbl_icono.pack(anchor="w", pady=(0, 4))

        lbl_sub = tk.Label(
            padre,
            text="La tarea se creó en Todoist y la evidencia está disponible en Drive.\n"
                 "El formato preestablecido para WhatsApp fue copiado automáticamente a tu portapapeles:",
            font=FONT_LABEL,
            fg=TEXT_MAIN,
            bg=BG_MAIN,
            justify="left"
        )
        lbl_sub.pack(anchor="w", pady=(0, 6))

        ruta_drive = resultado.get("ruta_drive", "")
        if ruta_drive:
            lbl_drive_path = tk.Label(
                padre,
                text=f"📂 Ubicación en Google Drive: {ruta_drive}",
                font=FONT_BOLD,
                fg="#38bdf8",
                bg=BG_MAIN
            )
            lbl_drive_path.pack(anchor="w", pady=(0, 8))

        # Caja de texto con el formato exacto para WhatsApp
        frame_text = tk.Frame(padre, bg=BG_INPUT, bd=1, relief=tk.SOLID, highlightbackground=BORDER_COLOR, highlightthickness=1)
        frame_text.pack(fill=tk.BOTH, expand=True, pady=(0, 12))

        self.txt_wa = tk.Text(
            frame_text,
            bg=BG_INPUT,
            fg=TEXT_MAIN,
            font=FONT_LABEL,
            insertbackground=TEXT_MAIN,
            bd=0,
            padx=10,
            pady=10,
            wrap=tk.WORD
        )
        self.txt_wa.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.txt_wa.insert(tk.END, mensaje_wa)

        scroll_txt = ttk.Scrollbar(frame_text, orient="vertical", command=self.txt_wa.yview)
        scroll_txt.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_wa.configure(yscrollcommand=scroll_txt.set)

        # Botón grande para copiar para WhatsApp
        self.btn_copiar = tk.Button(
            padre,
            text="📋  Copiar Nuevamente para WhatsApp (Ctrl + V)",
            font=FONT_SECTION,
            bg=ACCENT_GREEN,
            fg="#ffffff",
            activebackground="#059669",
            activeforeground="#ffffff",
            bd=0,
            pady=8,
            cursor="hand2",
            command=self.copiar_whatsapp
        )
        self.btn_copiar.pack(fill=tk.X, pady=(0, 12))

        # Fila con botones rápidos: Abrir Todoist, Abrir Drive
        row_links = tk.Frame(padre, bg=BG_MAIN)
        row_links.pack(fill=tk.X, pady=(0, 14))

        if task_url:
            btn_todoist = tk.Button(
                row_links,
                text="🔗  Abrir en Todoist",
                font=FONT_BOLD,
                bg=BG_CARD,
                fg=TEXT_MAIN,
                activebackground=ACCENT_BLUE,
                activeforeground=TEXT_MAIN,
                bd=1,
                relief=tk.SOLID,
                pady=6,
                cursor="hand2",
                command=lambda: webbrowser.open(task_url)
            )
            btn_todoist.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        if drive_link:
            btn_drive = tk.Button(
                row_links,
                text="☁️  Abrir en Google Drive",
                font=FONT_BOLD,
                bg=BG_CARD,
                fg=TEXT_MAIN,
                activebackground=ACCENT_BLUE,
                activeforeground=TEXT_MAIN,
                bd=1,
                relief=tk.SOLID,
                pady=6,
                cursor="hand2",
                command=lambda: webbrowser.open(drive_link)
            )
            btn_drive.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        # Fila inferior: Revertir y Cerrar
        row_bottom = tk.Frame(padre, bg=BG_MAIN)
        row_bottom.pack(fill=tk.X)

        btn_revertir = tk.Button(
            row_bottom,
            text="🔄  Revertir / Deshacer Reporte",
            font=FONT_LABEL,
            bg=BG_CARD,
            fg=ACCENT_RED,
            activebackground=ACCENT_RED,
            activeforeground="#ffffff",
            bd=1,
            relief=tk.SOLID,
            padx=12,
            pady=5,
            cursor="hand2",
            command=self.confirmar_revertir
        )
        btn_revertir.pack(side=tk.LEFT)

        btn_cerrar = tk.Button(
            row_bottom,
            text="Listo / Cerrar",
            font=FONT_BOLD,
            bg=BG_CARD_LIGHT,
            fg=TEXT_MAIN,
            activebackground=BORDER_COLOR,
            activeforeground=TEXT_MAIN,
            bd=0,
            padx=20,
            pady=5,
            cursor="hand2",
            command=self.destroy
        )
        btn_cerrar.pack(side=tk.RIGHT)

    def copiar_whatsapp(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(getattr(self, "mensaje_wa", self.resultado.get("mensaje_whatsapp", "")))
            self.btn_copiar.configure(text="✅  ¡Copiado con Éxito al Portapapeles!", bg="#059669")
            self.after(2000, lambda: self.btn_copiar.configure(
                text="📋  Copiar Nuevamente para WhatsApp (Ctrl + V)",
                bg=ACCENT_GREEN
            ))
        except Exception:
            pass

    def confirmar_revertir(self):
        if messagebox.askyesno(
            "Confirmar Reversión",
            "¿Deseas revertir y eliminar este reporte?\n\n"
            "• Se eliminará la tarea creada en Todoist.\n"
            "• Se eliminará el archivo subido a Google Drive.\n"
            "• Se restaurará el nombre original del archivo.",
            parent=self
        ):
            self.destroy()
            if self.on_revertir:
                self.on_revertir(self.resultado)


class ModalSubirReporteQA:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("QA Automation Suite - Subir Reporte a Todoist")

        # Adaptar dimensiones al monitor (pantallas de 1280x720, 1080p, etc.)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        win_w = min(860, max(720, screen_w - 60))
        win_h = min(670, max(520, screen_h - 70))
        pos_x = max(0, (screen_w - win_w) // 2)
        pos_y = max(0, (screen_h - win_h - 40) // 2)

        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(700, 480)
        self.root.configure(bg=BG_MAIN)

        self.proceso_en_curso = False
        self.hilo_trabajo = None
        self.archivo_seleccionado = tk.StringVar()
        self.cfg_ia = cargar_configuracion_ia()
        self.proveedor_ia = tk.StringVar(value=self.cfg_ia.get("proveedor_activo", "gemini"))
        self.modelo_groq_nombre = tk.StringVar(value=list(MODELOS_GROQ_DISPONIBLES.keys())[0])
        self.groq_api_key_var = tk.StringVar(value=self.cfg_ia.get("groq_api_key", ""))
        self.modelo_seleccionado = tk.StringVar(value="3.6 Flash (Recomendado - Análisis de Video Nativo - 20 consultas/día gratis)")
        self.razonamiento_ampliado = tk.BooleanVar(value=False)
        self.lista_versiones = cargar_versiones_app()
        default_version = self.lista_versiones[0] if self.lista_versiones else "4.4.1.09debug"
        self.version_app = tk.StringVar(value=default_version)
        self.dispositivo = tk.StringVar(value="E800")
        self.prioridad = tk.IntVar(value=3)  # P3 por defecto
        self.dict_etiquetas = {}
        self.preanalisis_en_curso = False
        self.modulo_producto = tk.StringVar(value="Auto-detectar (Según etiquetas y descripción)")

        self.configurar_estilos_ttk()
        self.construir_interfaz()
        self.actualizar_indicador_cuota()
        self.actualizar_preview_destino_drive()
        self.version_app.trace_add("write", lambda *a: self.actualizar_preview_destino_drive())
        self.vincular_eventos()
        self.cargar_evidencias_recientes()

        # Configurar Drag & Drop nativo con windnd
        self.configurar_drag_and_drop()

        # Refrescar el indicador de cuota en tiempo real cada 10 segundos
        self._iniciar_refresco_cuota_periodico()

    def configurar_estilos_ttk(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=BG_MAIN, foreground=TEXT_MAIN, font=FONT_LABEL)
        
        # TCombobox
        style.configure(
            "TCombobox",
            fieldbackground=BG_INPUT,
            background=BG_CARD_LIGHT,
            foreground=TEXT_MAIN,
            arrowcolor=TEXT_MAIN,
            bordercolor=BORDER_COLOR,
            lightcolor=BORDER_COLOR,
            darkcolor=BORDER_COLOR,
            padding=5
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", BG_INPUT)],
            selectbackground=[("readonly", BG_CARD_LIGHT)],
            selectforeground=[("readonly", TEXT_MAIN)]
        )

        # TProgressbar con barra azul moderna
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=BG_INPUT,
            background=ACCENT_BLUE,
            bordercolor=BORDER_COLOR,
            lightcolor=ACCENT_BLUE,
            darkcolor=ACCENT_BLUE,
            thickness=12
        )

        # Vertical.TScrollbar moderna
        style.configure(
            "Vertical.TScrollbar",
            troughcolor=BG_MAIN,
            background=BG_CARD_LIGHT,
            bordercolor=BG_MAIN,
            arrowcolor=TEXT_MUTED
        )

    def construir_interfaz(self):
        # Contenedor con Canvas y Scrollbar Vertical para permitir desplazarse arriba y abajo
        self.canvas = tk.Canvas(self.root, bg=BG_MAIN, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview, style="Vertical.TScrollbar")

        self.main_frame = tk.Frame(self.canvas, bg=BG_MAIN, padx=18, pady=12)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.main_frame, anchor="nw")

        def _actualizar_scrollregion(event=None):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        self.main_frame.bind("<Configure>", _actualizar_scrollregion)

        def _on_canvas_configure(event):
            # Expande el ancho del frame interno al redimensionar o maximizar la ventana
            self.canvas.itemconfig(self.canvas_window, width=event.width)

        self.canvas.bind("<Configure>", _on_canvas_configure)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # -------------------------------------------------------------
        # 1. ENCABEZADO
        # -------------------------------------------------------------
        header_frame = tk.Frame(self.main_frame, bg=BG_MAIN)
        header_frame.pack(fill=tk.X, pady=(0, 10))

        lbl_titulo = tk.Label(
            header_frame,
            text="🛠️  Generador y Publicador de Reportes de QA",
            font=FONT_TITLE,
            fg=TEXT_MAIN,
            bg=BG_MAIN
        )
        lbl_titulo.pack(anchor="w")

        lbl_subtitulo = tk.Label(
            header_frame,
            text="Análisis de Video o Imagen con Gemini IA  •  Alojamiento en Google Drive  •  Publicación en Todoist",
            font=FONT_SUBTITLE,
            fg=TEXT_MUTED,
            bg=BG_MAIN
        )
        lbl_subtitulo.pack(anchor="w", pady=(2, 0))

        # -------------------------------------------------------------
        # 2. SECCIÓN: EVIDENCIA (DRAG & DROP + RUTA)
        # -------------------------------------------------------------
        self.card_evidencia = tk.LabelFrame(
            self.main_frame,
            text=" 📁 1. Evidencia de Prueba (Video o Imagen) ",
            font=FONT_SECTION,
            fg=ACCENT_BLUE,
            bg=BG_CARD,
            bd=1,
            relief=tk.SOLID,
            padx=14,
            pady=8
        )
        self.card_evidencia.pack(fill=tk.X, pady=(0, 10))

        # Dropzone interactivo
        self.dropzone = tk.Frame(
            self.card_evidencia,
            bg=BG_INPUT,
            highlightthickness=2,
            highlightbackground=BORDER_COLOR,
            highlightcolor=ACCENT_BLUE,
            cursor="hand2",
            padx=10,
            pady=10
        )
        self.dropzone.pack(fill=tk.X, pady=(0, 6))
        self.dropzone.bind("<Button-1>", lambda e: self.examinar_archivo())

        self.lbl_drop_icon = tk.Label(
            self.dropzone,
            text="📥 Arrastra y suelta aquí tu archivo de video (.mp4) o imagen (.png, .jpg)",
            font=FONT_BOLD,
            fg=TEXT_MAIN,
            bg=BG_INPUT
        )
        self.lbl_drop_icon.pack()
        self.lbl_drop_icon.bind("<Button-1>", lambda e: self.examinar_archivo())

        self.lbl_drop_sub = tk.Label(
            self.dropzone,
            text="O haz clic aquí o en 'Examinar...' para buscar en tu equipo",
            font=FONT_SUBTITLE,
            fg=TEXT_MUTED,
            bg=BG_INPUT
        )
        self.lbl_drop_sub.pack(pady=(2, 0))
        self.lbl_drop_sub.bind("<Button-1>", lambda e: self.examinar_archivo())

        # Fila para entrada de texto y botón examinar
        row_ruta = tk.Frame(self.card_evidencia, bg=BG_CARD)
        row_ruta.pack(fill=tk.X, pady=(0, 6))

        self.entry_archivo = tk.Entry(
            row_ruta,
            textvariable=self.archivo_seleccionado,
            font=FONT_LABEL,
            bg=BG_INPUT,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            bd=1,
            relief=tk.SOLID
        )
        self.entry_archivo.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8))

        btn_examinar = tk.Button(
            row_ruta,
            text="🔍 Examinar...",
            font=FONT_BOLD,
            bg=BG_CARD_LIGHT,
            fg=TEXT_MAIN,
            activebackground=ACCENT_BLUE,
            activeforeground=TEXT_MAIN,
            bd=0,
            padx=14,
            pady=4,
            cursor="hand2",
            command=self.examinar_archivo
        )
        btn_examinar.pack(side=tk.RIGHT)

        # Fila de archivos recientes detectados
        row_recientes = tk.Frame(self.card_evidencia, bg=BG_CARD)
        row_recientes.pack(fill=tk.X)

        lbl_recientes = tk.Label(
            row_recientes,
            text="Evidencias recientes en Reportes QA:",
            font=FONT_SUBTITLE,
            fg=TEXT_MUTED,
            bg=BG_CARD
        )
        lbl_recientes.pack(side=tk.LEFT, padx=(0, 8))

        self.combo_recientes = ttk.Combobox(
            row_recientes,
            state="readonly",
            font=FONT_LABEL
        )
        self.combo_recientes.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.combo_recientes.bind("<<ComboboxSelected>>", self.on_reciente_seleccionado)

        # Estado interactivo del Pre-análisis Inteligente con IA
        self.lbl_estado_ia = tk.Label(
            self.card_evidencia,
            text="",
            font=FONT_BOLD,
            fg="#38bdf8",
            bg=BG_CARD,
            wraplength=780,
            justify="left"
        )
        self.lbl_estado_ia.pack(anchor="w", pady=(6, 0))

        # -------------------------------------------------------------
        # 3. SECCIÓN: DETALLES Y ENTORNO
        # -------------------------------------------------------------
        card_detalles = tk.LabelFrame(
            self.main_frame,
            text=" 📝 2. Detalles del Error y Configuración ",
            font=FONT_SECTION,
            fg=ACCENT_BLUE,
            bg=BG_CARD,
            bd=1,
            relief=tk.SOLID,
            padx=14,
            pady=10
        )
        card_detalles.pack(fill=tk.X, pady=(0, 12))

        # Descripción textual del problema
        lbl_desc = tk.Label(
            card_detalles,
            text="Descripción o exposición del problema (Obligatorio para imágenes):",
            font=FONT_LABEL,
            fg=TEXT_MAIN,
            bg=BG_CARD
        )
        lbl_desc.pack(anchor="w", pady=(0, 4))

        self.txt_descripcion = tk.Text(
            card_detalles,
            height=3,
            font=FONT_LABEL,
            bg=BG_INPUT,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            bd=1,
            relief=tk.SOLID,
            wrap=tk.WORD
        )
        self.txt_descripcion.pack(fill=tk.X, pady=(0, 10))
        self.txt_descripcion.bind("<KeyRelease>", lambda e: self.actualizar_preview_destino_drive())

        # =============================================================
        # Contenedor Multi-IA: Selector Dual de Proveedor (DeepSeek vs Gemini)
        # =============================================================
        self.frame_proveedor_ia = tk.LabelFrame(
            card_detalles,
            text=" 🧠 Motor de Inteligencia Artificial para Análisis de Evidencia ",
            font=FONT_SECTION,
            fg="#c084fc",
            bg=BG_CARD,
            bd=1,
            relief=tk.SOLID,
            padx=12,
            pady=8
        )
        self.frame_proveedor_ia.pack(fill=tk.X, pady=(0, 12))

        # Selector Radiobutton de Proveedor
        row_radios = tk.Frame(self.frame_proveedor_ia, bg=BG_CARD)
        row_radios.pack(fill=tk.X, pady=(0, 8))

        self.rb_gemini = tk.Radiobutton(
            row_radios,
            text="🔵 Google Gemini IA (Recomendado para Videos - 1,500 consultas/día gratis - Análisis Visual Nativo)",
            variable=self.proveedor_ia,
            value="gemini",
            font=FONT_BOLD,
            fg="#38bdf8",
            bg=BG_CARD,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground="#38bdf8",
            command=self.cambiar_proveedor_ia
        )
        self.rb_gemini.pack(anchor="w", pady=(0, 4))

        self.rb_deepseek = tk.Radiobutton(
            row_radios,
            text="🟣 Groq IA: OpenAI GPT-OSS 120B / Qwen (14,400 consultas/día gratis - Híbrido con Visión)",
            variable=self.proveedor_ia,
            value="deepseek_groq",
            font=FONT_BOLD,
            fg="#c084fc",
            bg=BG_CARD,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground="#c084fc",
            command=self.cambiar_proveedor_ia
        )
        self.rb_deepseek.pack(anchor="w")

        # -------------------------------------------------------------
        # PANEL A: DEEPSEEK R1 / GROQ
        # -------------------------------------------------------------
        self.panel_deepseek = tk.Frame(
            self.frame_proveedor_ia,
            bg=BG_INPUT,
            bd=1,
            relief=tk.SOLID,
            highlightbackground="#8b5cf6",
            highlightthickness=1,
            padx=12,
            pady=10
        )

        lbl_mod_groq = tk.Label(
            self.panel_deepseek,
            text="Modelo de Razonamiento en Groq Cloud:",
            font=FONT_LABEL,
            fg=TEXT_MUTED,
            bg=BG_INPUT
        )
        lbl_mod_groq.pack(anchor="w", pady=(0, 2))

        combo_groq = ttk.Combobox(
            self.panel_deepseek,
            textvariable=self.modelo_groq_nombre,
            values=list(MODELOS_GROQ_DISPONIBLES.keys()),
            state="readonly",
            font=FONT_LABEL
        )
        combo_groq.pack(fill=tk.X, pady=(0, 8))

        # Fila Clave Groq
        row_key = tk.Frame(self.panel_deepseek, bg=BG_INPUT)
        row_key.pack(fill=tk.X, pady=(0, 4))

        lbl_key = tk.Label(
            row_key,
            text="Clave de API Groq (gsk_...):",
            font=FONT_LABEL,
            fg=TEXT_MUTED,
            bg=BG_INPUT
        )
        lbl_key.pack(side=tk.LEFT)

        btn_get_key = tk.Label(
            row_key,
            text="🔑 Obtener gratis sin tarjeta en console.groq.com",
            font=("Segoe UI", 8, "underline"),
            fg="#38bdf8",
            bg=BG_INPUT,
            cursor="hand2"
        )
        btn_get_key.pack(side=tk.RIGHT)
        btn_get_key.bind("<Button-1>", lambda e: webbrowser.open("https://console.groq.com/keys"))

        row_key_entry = tk.Frame(self.panel_deepseek, bg=BG_INPUT)
        row_key_entry.pack(fill=tk.X, pady=(0, 6))

        self.entry_groq_key = tk.Entry(
            row_key_entry,
            textvariable=self.groq_api_key_var,
            show="*",
            font=FONT_MONO,
            bg=BG_CARD,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            bd=1,
            relief=tk.SOLID
        )
        self.entry_groq_key.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3, padx=(0, 8))

        self.btn_toggle_key = tk.Button(
            row_key_entry,
            text="👁️",
            font=FONT_LABEL,
            bg=BG_CARD_LIGHT,
            fg=TEXT_MAIN,
            activebackground=BG_CARD,
            activeforeground=TEXT_MAIN,
            bd=0,
            padx=8,
            cursor="hand2",
            command=self.toggle_ver_clave_groq
        )
        self.btn_toggle_key.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_guardar_key = tk.Button(
            row_key_entry,
            text="💾 Guardar Clave",
            font=FONT_BOLD,
            bg="#8b5cf6",
            fg="#ffffff",
            activebackground="#7c3aed",
            activeforeground="#ffffff",
            bd=0,
            padx=12,
            pady=3,
            cursor="hand2",
            command=self.guardar_clave_groq
        )
        self.btn_guardar_key.pack(side=tk.RIGHT)

        lbl_groq_badge = tk.Label(
            self.panel_deepseek,
            text="⚡ Modo Híbrido: Visión de video + Razonamiento GPT-OSS 120B en Groq (14,400 consultas/día gratis)",
            font=FONT_SUBTITLE,
            fg=ACCENT_GREEN,
            bg=BG_INPUT
        )
        lbl_groq_badge.pack(anchor="w", pady=(2, 0))

        # -------------------------------------------------------------
        # PANEL B: GOOGLE GEMINI IA
        # -------------------------------------------------------------
        self.panel_gemini = tk.Frame(self.frame_proveedor_ia, bg=BG_CARD)

        lbl_modelo = tk.Label(
            self.panel_gemini,
            text="Modelo de Gemini IA (Elige según tu cuota/créditos):",
            font=FONT_LABEL,
            fg=TEXT_MUTED,
            bg=BG_CARD
        )
        lbl_modelo.pack(anchor="w", pady=(0, 2))

        combo_modelo = ttk.Combobox(
            self.panel_gemini,
            textvariable=self.modelo_seleccionado,
            values=list(OPCIONES_MODELOS_GEMINI.keys()),
            state="readonly",
            font=FONT_LABEL
        )
        combo_modelo.pack(fill=tk.X)
        combo_modelo.bind("<<ComboboxSelected>>", self.actualizar_indicador_cuota)

        cb_thinking = tk.Checkbutton(
            self.panel_gemini,
            text="✓  Razonamiento ampliado (Resolución de problemas complejos y causa raíz)",
            variable=self.razonamiento_ampliado,
            font=FONT_BOLD,
            fg=ACCENT_BLUE,
            bg=BG_CARD,
            activebackground=BG_CARD,
            activeforeground=ACCENT_BLUE,
            selectcolor=BG_INPUT,
            highlightthickness=0,
            bd=0
        )
        cb_thinking.pack(anchor="w", pady=(5, 0))

        # Barra / Medidor visual de cuota y créditos de IA
        self.frame_cuota = tk.Frame(
            self.panel_gemini,
            bg=BG_INPUT,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=BORDER_COLOR,
            highlightthickness=1,
            padx=10,
            pady=7
        )
        self.frame_cuota.pack(fill=tk.X, pady=(8, 2))

        row_cuota_top = tk.Frame(self.frame_cuota, bg=BG_INPUT)
        row_cuota_top.pack(fill=tk.X)

        self.lbl_cuota_badge = tk.Label(row_cuota_top, text="", font=FONT_BOLD, fg=ACCENT_GREEN, bg=BG_INPUT)
        self.lbl_cuota_badge.pack(side=tk.LEFT)

        self.lbl_cuota_pct = tk.Label(row_cuota_top, text="", font=FONT_MONO, fg=TEXT_MUTED, bg=BG_INPUT)
        self.lbl_cuota_pct.pack(side=tk.RIGHT)

        self.btn_verificar_cuota = tk.Label(
            row_cuota_top,
            text=" 🔄 Comprobar en vivo ",
            font=("Segoe UI", 8, "underline"),
            fg="#38bdf8",
            bg=BG_INPUT,
            cursor="hand2"
        )
        self.btn_verificar_cuota.pack(side=tk.RIGHT, padx=(0, 10))
        self.btn_verificar_cuota.bind("<Button-1>", lambda e: self.verificar_cuota_en_vivo())

        # Círculo semáforo de saturación del servidor
        self.canvas_semaforo = tk.Canvas(
            row_cuota_top,
            width=14, height=14,
            bg=BG_INPUT, bd=0, highlightthickness=0
        )
        self.canvas_semaforo.pack(side=tk.RIGHT, padx=(0, 4))
        # Tooltip explicativo
        self._semaforo_ovalo = self.canvas_semaforo.create_oval(2, 2, 12, 12, fill="#64748b", outline="")
        self._semaforo_estado = "desconocido"
        self.canvas_semaforo.bind("<Enter>", self._mostrar_tooltip_semaforo)
        self.canvas_semaforo.bind("<Leave>", self._ocultar_tooltip_semaforo)
        self._tooltip_semaforo_win = None

        self.lbl_semaforo_texto = tk.Label(
            row_cuota_top,
            text="",
            font=("Segoe UI", 8, "bold"),
            fg="#64748b",
            bg=BG_INPUT
        )
        self.lbl_semaforo_texto.pack(side=tk.RIGHT, padx=(0, 6))

        # Barra visual Canvas
        self.canvas_cuota = tk.Canvas(self.frame_cuota, height=8, bg=BG_CARD_LIGHT, bd=0, highlightthickness=0)
        self.canvas_cuota.pack(fill=tk.X, pady=(5, 4))
        self.canvas_cuota.bind("<Configure>", lambda e: self.actualizar_indicador_cuota())

        self.lbl_cuota_detalle = tk.Label(self.frame_cuota, text="", font=FONT_SUBTITLE, fg=TEXT_MUTED, bg=BG_INPUT, justify="left")
        self.lbl_cuota_detalle.pack(anchor="w")

        # Empaquetar el panel inicial según la configuración guardada
        self.cambiar_proveedor_ia()

        # Fila 50/50 simétrica: Versión App y Dispositivo
        row_entorno = tk.Frame(card_detalles, bg=BG_CARD)
        row_entorno.pack(fill=tk.X, pady=(0, 10))

        # Columna Izquierda: Versión App con botón para eliminar obsoletas
        col_version = tk.Frame(row_entorno, bg=BG_CARD)
        col_version.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        lbl_version = tk.Label(col_version, text="Versión de la App:", font=FONT_LABEL, fg=TEXT_MUTED, bg=BG_CARD)
        lbl_version.pack(anchor="w", pady=(0, 2))

        frame_ver_input = tk.Frame(col_version, bg=BG_CARD)
        frame_ver_input.pack(fill=tk.X)

        self.combo_version = ttk.Combobox(
            frame_ver_input,
            textvariable=self.version_app,
            values=self.lista_versiones,
            font=FONT_LABEL
        )
        self.combo_version.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_del_version = tk.Button(
            frame_ver_input,
            text=" 🗑️ ",
            font=("Segoe UI", 8),
            bg=BG_CARD_LIGHT,
            fg="#f87171",
            activebackground=ACCENT_RED,
            activeforeground="white",
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self.eliminar_version_actual
        )
        self.btn_del_version.pack(side=tk.RIGHT, padx=(4, 0))

        # Columna Derecha: Dispositivo
        col_device = tk.Frame(row_entorno, bg=BG_CARD)
        col_device.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(8, 0))

        lbl_device = tk.Label(col_device, text="Dispositivo utilizado:", font=FONT_LABEL, fg=TEXT_MUTED, bg=BG_CARD)
        lbl_device.pack(anchor="w", pady=(0, 2))

        combo_device = ttk.Combobox(
            col_device,
            textvariable=self.dispositivo,
            values=["E800", "PAX A920Pro", "PAX A920", "PAX Aries8", "Sunmi V2", "Sunmi T2"],
            font=FONT_LABEL
        )
        combo_device.pack(fill=tk.X)

        # Fila Módulo / Destino Inteligente en Google Drive
        row_drive = tk.Frame(card_detalles, bg=BG_CARD)
        row_drive.pack(fill=tk.X, pady=(0, 10))

        lbl_drive_prod = tk.Label(
            row_drive,
            text="📁 Módulo / Destino en Google Drive (Carpeta Soportes):",
            font=FONT_LABEL,
            fg=TEXT_MUTED,
            bg=BG_CARD
        )
        lbl_drive_prod.pack(anchor="w", pady=(0, 2))

        self.combo_producto_drive = ttk.Combobox(
            row_drive,
            textvariable=self.modulo_producto,
            values=[
                "Auto-detectar (Según etiquetas y descripción)",
                "Standard",
                "Air",
                "Backend",
                "KDS",
                "Kiosk",
                "Express"
            ],
            state="readonly",
            font=FONT_LABEL
        )
        self.combo_producto_drive.pack(fill=tk.X, pady=(0, 4))
        self.combo_producto_drive.bind("<<ComboboxSelected>>", self.actualizar_preview_destino_drive)

        self.lbl_destino_drive = tk.Label(
            row_drive,
            text="",
            font=FONT_BOLD,
            fg="#38bdf8",
            bg=BG_CARD,
            wraplength=780,
            justify="left"
        )
        self.lbl_destino_drive.pack(anchor="w")

        # Prioridad en Todoist (4 columnas simétricas)
        lbl_prio = tk.Label(card_detalles, text="Prioridad en Todoist:", font=FONT_LABEL, fg=TEXT_MUTED, bg=BG_CARD)
        lbl_prio.pack(anchor="w", pady=(0, 4))

        row_prio = tk.Frame(card_detalles, bg=BG_CARD)
        row_prio.pack(fill=tk.X, pady=(0, 10))

        opciones_prio = [
            (1, "🔴 P1 Urgente", ACCENT_RED),
            (2, "🟠 P2 Alta", ACCENT_YELLOW),
            (3, "🟡 P3 Media", "#facc15"),
            (4, "⚪ P4 Baja", TEXT_MUTED)
        ]
        for idx, (val, txt, col) in enumerate(opciones_prio):
            rb = tk.Radiobutton(
                row_prio,
                text=txt,
                variable=self.prioridad,
                value=val,
                font=FONT_LABEL,
                fg=col,
                bg=BG_CARD,
                activebackground=BG_CARD,
                activeforeground=col,
                selectcolor=BG_INPUT,
                highlightthickness=0,
                bd=0
            )
            rb.pack(side=tk.LEFT, expand=True, anchor="w")

        # Matriz simétrica de Etiquetas (4 columnas x 3 filas)
        lbl_tags = tk.Label(card_detalles, text="Etiquetas del reporte:", font=FONT_LABEL, fg=TEXT_MUTED, bg=BG_CARD)
        lbl_tags.pack(anchor="w", pady=(0, 4))

        grid_tags = tk.Frame(card_detalles, bg=BG_CARD)
        grid_tags.pack(fill=tk.X)

        columnas_etiquetas = [
            ["Bug", "Apk Air", "Backend"],
            ["Apk Standard", "Apk Air 2.0", "Frontend"],
            ["Offline", "Apk Kiosk", "En Español"],
            ["Online", "Apk Kds", "Old"]
        ]

        etiquetas_activas_default = {"Bug", "Apk Standard", "Offline"}

        for col_idx, columna in enumerate(columnas_etiquetas):
            grid_tags.columnconfigure(col_idx, weight=1)
            for row_idx, tag in enumerate(columna):
                var = tk.BooleanVar(value=(tag in etiquetas_activas_default))
                self.dict_etiquetas[tag] = var
                cb = tk.Checkbutton(
                    grid_tags,
                    text=tag,
                    variable=var,
                    font=FONT_LABEL,
                    fg=TEXT_MAIN,
                    bg=BG_CARD,
                    activebackground=BG_CARD,
                    activeforeground=TEXT_MAIN,
                    selectcolor=BG_INPUT,
                    highlightthickness=0,
                    bd=0,
                    command=self.actualizar_preview_destino_drive
                )
                cb.grid(row=row_idx, column=col_idx, sticky="w", pady=2)

        # -------------------------------------------------------------
        # 4. BOTONES DE ACCIÓN (SIMÉTRICOS)
        # -------------------------------------------------------------
        row_botones = tk.Frame(self.main_frame, bg=BG_MAIN)
        row_botones.pack(fill=tk.X, pady=(0, 12))

        self.btn_generar = tk.Button(
            row_botones,
            text="🚀  Generar y Subir Reporte a Todoist",
            font=("Segoe UI", 11, "bold"),
            bg=ACCENT_BLUE,
            fg="#ffffff",
            activebackground=ACCENT_BLUE_HOVER,
            activeforeground="#ffffff",
            bd=0,
            pady=10,
            cursor="hand2",
            command=self.iniciar_proceso
        )
        self.btn_generar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.btn_cancelar = tk.Button(
            row_botones,
            text="⛔  Cancelar (ESC)",
            font=("Segoe UI", 10, "bold"),
            bg=ACCENT_RED,
            fg="#ffffff",
            activebackground=ACCENT_RED_HOVER,
            activeforeground="#ffffff",
            bd=0,
            pady=10,
            padx=18,
            cursor="hand2",
            command=self.cancelar_proceso
        )
        self.btn_cancelar.pack(side=tk.RIGHT)

        # -------------------------------------------------------------
        # 5. MONITOREO DEL PROCEDIMIENTO EN VIVO (SIN SUPERPOSICIÓN)
        # -------------------------------------------------------------
        self.card_monitoreo = tk.LabelFrame(
            self.main_frame,
            text=" 📊 3. Monitoreo del Procedimiento en Vivo ",
            font=FONT_SECTION,
            fg=ACCENT_BLUE,
            bg=BG_CARD,
            bd=1,
            relief=tk.SOLID,
            padx=14,
            pady=10
        )
        self.card_monitoreo.pack(fill=tk.X, pady=(0, 10))

        # 4 Tarjetas de Pasos simétricas y nítidas (Sin emojis compuestos de teclado)
        self.frame_pasos = tk.Frame(self.card_monitoreo, bg=BG_CARD)
        self.frame_pasos.pack(fill=tk.X, pady=(0, 8))

        self.pasos_ui = {}
        pasos_info = [
            ("gemini_upload", "[1] Subir Archivo"),
            ("gemini_analysis", "[2] Análisis IA"),
            ("drive_upload", "[3] Google Drive"),
            ("todoist_create", "[4] Todoist")
        ]

        for idx, (clave, titulo) in enumerate(pasos_info):
            self.frame_pasos.columnconfigure(idx, weight=1)
            f_paso = tk.Frame(self.frame_pasos, bg=BG_INPUT, bd=1, relief=tk.SOLID, padx=6, pady=6)
            f_paso.grid(row=0, column=idx, sticky="ew", padx=3)

            lbl_t = tk.Label(f_paso, text=titulo, font=FONT_BOLD, fg=TEXT_MUTED, bg=BG_INPUT)
            lbl_t.pack()

            lbl_s = tk.Label(f_paso, text="⚪ Pendiente", font=FONT_SUBTITLE, fg=TEXT_MUTED, bg=BG_INPUT)
            lbl_s.pack(pady=(2, 0))

            self.pasos_ui[clave] = {"frame": f_paso, "titulo": lbl_t, "estado": lbl_s}

        # Barra de progreso estilizada
        self.progreso = ttk.Progressbar(
            self.card_monitoreo,
            style="Horizontal.TProgressbar",
            mode="determinate",
            maximum=100
        )
        self.progreso.pack(fill=tk.X, pady=(0, 8))

        # Consola de logs integrada
        self.txt_logs = tk.Text(
            self.card_monitoreo,
            height=5,
            font=FONT_MONO,
            bg=BG_INPUT,
            fg=TEXT_MUTED,
            bd=0,
            wrap=tk.WORD,
            state="disabled"
        )
        self.txt_logs.pack(fill=tk.X, pady=(0, 4))

        # Redirigir stdout/stderr a la consola visual
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr
        self.redirector_out = RedirectOutput(self.txt_logs, self.root, self.old_stdout)
        self.redirector_err = RedirectOutput(self.txt_logs, self.root, self.old_stderr)
        sys.stdout = self.redirector_out
        sys.stderr = self.redirector_err

    def configurar_drag_and_drop(self):
        """Habilita la funcionalidad de arrastrar y soltar nativa con windnd."""
        if not HAS_WINDND:
            self.log_mensaje("⚠️ windnd no está disponible. Usa el botón 'Examinar' para seleccionar archivos.")
            return

        def on_drop(archivos):
            if not archivos:
                return
            archivo_drop = archivos[0]
            if isinstance(archivo_drop, bytes):
                try:
                    archivo_drop = archivo_drop.decode("utf-8")
                except UnicodeDecodeError:
                    archivo_drop = archivo_drop.decode("mbcs", errors="replace")

            archivo_drop = archivo_drop.strip().strip('"')
            if os.path.isfile(archivo_drop):
                self.actualizar_archivo_seleccionado(archivo_drop)
                self.log_mensaje(f"📂 Archivo cargado mediante Drag & Drop: {os.path.basename(archivo_drop)}")
            else:
                messagebox.showwarning("Archivo Inválido", "El elemento arrastrado no es un archivo válido.")

        try:
            windnd.hook_dropfiles(self.root, func=on_drop, force_unicode=True)
            windnd.hook_dropfiles(self.canvas, func=on_drop, force_unicode=True)
            windnd.hook_dropfiles(self.dropzone, func=on_drop, force_unicode=True)
            windnd.hook_dropfiles(self.entry_archivo, func=on_drop, force_unicode=True)
        except Exception as e:
            self.log_mensaje(f"Nota: Drag & Drop nativo no pudo engancharse ({e}).")

    def vincular_eventos(self):
        self.root.bind("<Escape>", lambda event: self.cancelar_proceso())
        self.root.protocol("WM_DELETE_WINDOW", self.cerrar_ventana)
        # Habilitar desplazamiento con rueda del ratón en toda la ventana
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        try:
            if not self.canvas.winfo_exists():
                return
            widget = event.widget
            if widget == self.txt_logs:
                widget.yview_scroll(int(-1 * (event.delta / 120)), "units")
            else:
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def eliminar_version_actual(self):
        """Permite al usuario eliminar versiones obsoletas de la lista desplegable."""
        actual = self.version_app.get().strip()
        if not actual:
            messagebox.showwarning("Atención", "No hay ninguna versión seleccionada para eliminar.", parent=self.root)
            return

        if actual not in self.lista_versiones:
            messagebox.showinfo("Información", f"La versión '{actual}' no está en la lista guardada.", parent=self.root)
            return

        if len(self.lista_versiones) <= 1:
            messagebox.showwarning("Atención", "Debe quedar al menos una versión en la lista.", parent=self.root)
            return

        confirmar = messagebox.askyesno(
            "Eliminar Versión Obsoleta",
            f"¿Deseas eliminar la versión obsoleta '{actual}' de la lista desplegable?",
            parent=self.root
        )
        if confirmar:
            self.lista_versiones.remove(actual)
            guardar_versiones_app(self.lista_versiones)
            self.combo_version.configure(values=self.lista_versiones)
            nueva = self.lista_versiones[0]
            self.version_app.set(nueva)
            print(f"🗑️ Versión obsoleta eliminada: {actual}. Nueva versión seleccionada: {nueva}")

    def agregar_version_si_no_existe(self, nueva_version: str):
        """Agrega automáticamente una versión detectada a la lista si aún no existe."""
        nueva = nueva_version.strip()
        if nueva and nueva not in self.lista_versiones:
            self.lista_versiones.insert(0, nueva)
            guardar_versiones_app(self.lista_versiones)
            if hasattr(self, "combo_version") and self.combo_version.winfo_exists():
                self.combo_version.configure(values=self.lista_versiones)

    def _medir_saturacion_servidor(self, modelo_id: str, api_key: str, prueba_profunda: bool = False) -> str:
        """
        Mide la disponibilidad y saturación del servidor de la IA seleccionada.
        Retorna:
          'verde': Alta disponibilidad — se puede usar sin riesgo de que no responda el bot.
          'amarillo': Disponibilidad media — probable que se pueda usar (~50%).
          'rojo': Saturado (503), sin cuota (429) o inaccesible — nunca se puede usar en este momento.
        """
        prov = self.proveedor_ia.get() if hasattr(self, "proveedor_ia") else "gemini"

        try:
            if prov == "deepseek_groq":
                groq_key = self.groq_api_key_var.get().strip() if hasattr(self, "groq_api_key_var") else ""
                if not groq_key:
                    return "gris"
                url = "https://api.groq.com/openai/v1/models"
                headers = {"Authorization": f"Bearer {groq_key}"}
                t0 = time.monotonic()
                r = requests.get(url, headers=headers, timeout=5.0)
                latencia = (time.monotonic() - t0) * 1000
                if r.status_code == 200:
                    return "verde" if latencia < 1500 else "amarillo"
                elif r.status_code in (429, 503):
                    return "rojo"
                else:
                    return "amarillo"
            else:
                # Comprobar primero el estado registrado en gestor de cuotas
                estado_local = gestor_cuotas.obtener_saturacion_modelo(modelo_id)
                if estado_local == "rojo" and not prueba_profunda:
                    return "rojo"

                if prueba_profunda:
                    # Prueba en vivo: verificación rápida con generación mínima para detectar 503
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo_id}:generateContent?key={api_key}"
                    payload = {"contents": [{"parts": [{"text": "p"}]}], "generationConfig": {"maxOutputTokens": 1}}
                    r = requests.post(url, json=payload, timeout=12.0)
                    if r.status_code == 200:
                        gestor_cuotas.registrar_exito(modelo_id)
                        return "verde"
                    elif r.status_code == 503:
                        gestor_cuotas.registrar_503(modelo_id, "503 Servidor saturado por alta demanda en Google")
                        return "rojo"
                    elif r.status_code == 429:
                        gestor_cuotas.registrar_429(modelo_id, "429 Cuota agotada")
                        return "rojo"
                    else:
                        return "amarillo"
                else:
                    # Sondeo automático periódico: GET metadata para NO gastar tokens
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo_id}?key={api_key}"
                    t0 = time.monotonic()
                    r = requests.get(url, timeout=5.0)
                    latencia = (time.monotonic() - t0) * 1000
                    if r.status_code == 200:
                        if estado_local == "rojo":
                            return "rojo"
                        if estado_local == "amarillo":
                            return "amarillo"
                        return "verde" if latencia < 1800 else "amarillo"
                    elif r.status_code == 429:
                        gestor_cuotas.registrar_429(modelo_id)
                        return "rojo"
                    elif r.status_code == 503:
                        gestor_cuotas.registrar_503(modelo_id)
                        return "rojo"
                    else:
                        return "amarillo"
        except requests.exceptions.Timeout:
            return "amarillo"
        except Exception:
            return "amarillo"

    def _actualizar_semaforo(self, estado: str):
        """Pinta el círculo semáforo según la disponibilidad real y actualiza el texto."""
        COLORES = {
            "verde":    "#10b981",   # Verde: sin riesgo de fallo
            "amarillo": "#f59e0b",   # Amarillo: probable que se pueda usar (~50%)
            "rojo":     "#ef4444",   # Rojo: no se puede usar (saturado / sin cuota)
            "gris":     "#64748b",   # Gris: sin info
            "desconocido": "#64748b",
        }
        TEXTOS_ESTADO = {
            "verde":    "🟢 Sin riesgo",
            "amarillo": "🟡 Probable",
            "rojo":     "🔴 Saturado",
            "gris":     "⚪ Sin datos",
            "desconocido": "⚪ Verificando...",
        }
        color = COLORES.get(estado, "#64748b")
        texto = TEXTOS_ESTADO.get(estado, "⚪ Verificando...")
        self._semaforo_estado = estado
        try:
            if hasattr(self, "canvas_semaforo") and self.canvas_semaforo.winfo_exists():
                self.canvas_semaforo.itemconfig(self._semaforo_ovalo, fill=color)
            if hasattr(self, "lbl_semaforo_texto") and self.lbl_semaforo_texto.winfo_exists():
                self.lbl_semaforo_texto.configure(text=texto, fg=color)
        except Exception:
            pass

    def _mostrar_tooltip_semaforo(self, event=None):
        """Muestra un tooltip explicativo al pasar el mouse sobre el semáforo."""
        textos = {
            "verde":    "🟢 Alta disponibilidad — Se puede usar sin ningún riesgo de que no responda el bot",
            "amarillo": "🟡 Disponibilidad media — Probable que se pueda usar (~50% de probabilidad de respuesta)",
            "rojo":     "🔴 Servidor saturado o sin cuota — No disponible ahora (Usa 3.6 Flash o Groq)",
            "gris":     "⚪ Sin datos — Pulsa 'Comprobar en vivo' para verificar",
            "desconocido": "⚪ Comprobando disponibilidad del servidor...",
        }
        texto = textos.get(self._semaforo_estado, textos["desconocido"])
        try:
            if self._tooltip_semaforo_win and self._tooltip_semaforo_win.winfo_exists():
                return
            x = self.canvas_semaforo.winfo_rootx() + 16
            y = self.canvas_semaforo.winfo_rooty() - 28
            win = tk.Toplevel(self.root)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{x}+{y}")
            win.configure(bg="#1e293b")
            tk.Label(
                win, text=texto,
                font=("Segoe UI", 8), fg="white", bg="#1e293b",
                padx=8, pady=4
            ).pack()
            self._tooltip_semaforo_win = win
        except Exception:
            pass

    def _ocultar_tooltip_semaforo(self, event=None):
        """Cierra el tooltip del semáforo."""
        try:
            if self._tooltip_semaforo_win and self._tooltip_semaforo_win.winfo_exists():
                self._tooltip_semaforo_win.destroy()
            self._tooltip_semaforo_win = None
        except Exception:
            pass

    def verificar_cuota_en_vivo(self):
        modelo_nombre = self.modelo_seleccionado.get()
        modelo_id = OPCIONES_MODELOS_GEMINI.get(modelo_nombre, "gemini-3.6-flash")
        if hasattr(self, "btn_verificar_cuota") and self.btn_verificar_cuota.winfo_exists():
            self.btn_verificar_cuota.configure(text=" ⏳ Comprobando... ", fg=ACCENT_YELLOW)
        self._actualizar_semaforo("desconocido")

        def _worker():
            api_key = gestor_pool.obtener_clave_activa()[0]
            # 1. Medir saturación real del servidor con prueba profunda
            estado_saturacion = self._medir_saturacion_servidor(modelo_id, api_key, prueba_profunda=True)
            # 2. Verificar estado de cuota en Google
            gestor_cuotas.probar_en_vivo_modelo(modelo_id, api_key)

            def _done():
                if hasattr(self, "btn_verificar_cuota") and self.btn_verificar_cuota.winfo_exists():
                    self.btn_verificar_cuota.configure(text=" 🔄 Comprobar en vivo ", fg="#38bdf8")
                self._actualizar_semaforo(estado_saturacion)
                self.actualizar_indicador_cuota()
            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def actualizar_indicador_cuota(self, event=None):
        modelo_nombre = self.modelo_seleccionado.get()
        modelo_id = OPCIONES_MODELOS_GEMINI.get(modelo_nombre, "gemini-3.6-flash")
        info = gestor_cuotas.obtener_estado_modelo(modelo_id)

        if hasattr(self, "lbl_cuota_badge") and self.lbl_cuota_badge.winfo_exists():
            self.lbl_cuota_badge.configure(text=info["badge"], fg=info["color"])
            self.lbl_cuota_pct.configure(text=info["porcentaje_str"], fg=info["color"])
            self.lbl_cuota_detalle.configure(text=info["detalle"])

            # Redibujar barra Canvas en tiempo real
            self.canvas_cuota.delete("all")
            w = self.canvas_cuota.winfo_width()
            if w <= 1:
                w = 380
            pct = info["porcentaje"]
            if pct > 0.0:
                ancho_lleno = max(4, int(w * pct))
                self.canvas_cuota.create_rectangle(0, 0, ancho_lleno, 8, fill=info["color"], width=0)
            else:
                self.canvas_cuota.create_rectangle(0, 0, w, 8, fill="#334155", width=0)

            # Actualizar inmediatamente el semáforo para el modelo seleccionado
            estado_sat = gestor_cuotas.obtener_saturacion_modelo(modelo_id)
            self._actualizar_semaforo(estado_sat)

    def cambiar_proveedor_ia(self):
        prov = self.proveedor_ia.get()
        if prov == "deepseek_groq":
            self.panel_gemini.pack_forget()
            self.panel_deepseek.pack(fill=tk.X, pady=(6, 0))
            self.frame_proveedor_ia.configure(fg="#c084fc")
        else:
            self.panel_deepseek.pack_forget()
            self.panel_gemini.pack(fill=tk.X, pady=(6, 0))
            self.frame_proveedor_ia.configure(fg="#38bdf8")
            self.actualizar_indicador_cuota()

        self.cfg_ia["proveedor_activo"] = prov
        guardar_configuracion_ia(self.cfg_ia)
        if hasattr(self, "canvas"):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _iniciar_refresco_cuota_periodico(self):
        """
        Actualiza el indicador de cuota y disponibilidad periódicamente sin congelar la UI ni gastar créditos.
        """
        try:
            if not hasattr(self, "root") or not self.root.winfo_exists():
                return

            self.actualizar_indicador_cuota()

            def _sondeo():
                modelo_nombre = self.modelo_seleccionado.get()
                modelo_id = OPCIONES_MODELOS_GEMINI.get(modelo_nombre, "gemini-3.6-flash")
                api_key = gestor_pool.obtener_clave_activa()[0]
                estado = self._medir_saturacion_servidor(modelo_id, api_key, prueba_profunda=False)
                try:
                    if self.root.winfo_exists():
                        self.root.after(0, lambda: self._actualizar_semaforo(estado))
                except Exception:
                    pass

            threading.Thread(target=_sondeo, daemon=True).start()
            self.root.after(12000, self._iniciar_refresco_cuota_periodico)
        except Exception:
            pass

    def _limpiar_formulario_completo(self):
        """
        Limpia todos los campos del formulario después de un reporte exitoso.
        Prepara la ventana para el siguiente reporte sin necesidad de reiniciar.
        """
        try:
            # Limpiar la ruta del archivo / video
            if hasattr(self, "var_ruta_archivo"):
                self.var_ruta_archivo.set("")
            if hasattr(self, "lbl_archivo_seleccionado"):
                self.lbl_archivo_seleccionado.configure(
                    text="Arrastra tu video o imagen aquí, o haz clic para seleccionar",
                    fg=TEXT_MUTED
                )

            # Limpiar descripción del problema
            if hasattr(self, "txt_descripcion"):
                self.txt_descripcion.delete("1.0", tk.END)

            # Limpiar estado de pre-análisis de IA
            if hasattr(self, "lbl_estado_ia"):
                self.lbl_estado_ia.configure(text="", fg=TEXT_MUTED)

            # Resetear prioridad a P3 (Media) por defecto
            if hasattr(self, "prioridad"):
                self.prioridad.set(3)

            # Limpiar log de la terminal (limpieza visual)
            if hasattr(self, "txt_log"):
                self.txt_log.configure(state="normal")
                self.txt_log.delete("1.0", tk.END)
                self.txt_log.configure(state="disabled")

            # Resetear pasos de proceso a pendiente
            for clave in self.pasos_ui:
                self.actualizar_paso(clave, "⚪", "Pendiente", BORDER_COLOR)

            # Actualizar cuota en tiempo real
            self.actualizar_indicador_cuota()
            self.preanalisis_en_curso = False

            self.log_mensaje("✅ Formulario limpiado. Listo para el siguiente reporte.")
        except Exception as e:
            pass

    def toggle_ver_clave_groq(self):
        if self.entry_groq_key.cget("show") == "*":
            self.entry_groq_key.configure(show="")
            self.btn_toggle_key.configure(text="🔒")
        else:
            self.entry_groq_key.configure(show="*")
            self.btn_toggle_key.configure(text="👁️")

    def guardar_clave_groq(self):
        key = self.groq_api_key_var.get().strip()
        if not key:
            messagebox.showwarning("Clave Vacía", "Por favor ingresa tu API Key de Groq (gsk_...). Puedes obtenerla gratis sin tarjeta en console.groq.com/keys.")
            return
        self.cfg_ia["groq_api_key"] = key
        guardar_configuracion_ia(self.cfg_ia)
        self.btn_guardar_key.configure(text="✅ Guardada", bg=ACCENT_GREEN)
        self.root.after(2000, lambda: self.btn_guardar_key.configure(text="💾 Guardar Clave", bg="#8b5cf6"))
        self.log_mensaje("🔑 Clave de API de Groq guardada exitosamente en config_ia.json.")

    def actualizar_preview_destino_drive(self, *args):
        if not hasattr(self, "lbl_destino_drive") or not self.lbl_destino_drive.winfo_exists():
            return
        tags = [t for t, v in self.dict_etiquetas.items() if v.get()] if hasattr(self, "dict_etiquetas") else []
        desc = self.txt_descripcion.get("1.0", tk.END).strip() if hasattr(self, "txt_descripcion") else ""
        prod_forzado = self.modulo_producto.get() if hasattr(self, "modulo_producto") else "Auto"
        prod = core.determinar_producto(etiquetas=tags, descripcion=desc, producto_forzado=prod_forzado)
        ver = self.version_app.get().strip() if hasattr(self, "version_app") else ""

        if prod == "Backend":
            ruta_texto = "Soportes / Backend"
        else:
            match = re.match(r"^(\d+\.\d+)\.(\d+)(?:\.([0-9a-zA-Z]+))?", ver)
            if match:
                n1 = match.group(1)
                n2 = f".{match.group(2)}"
                b_raw = match.group(3)
                if b_raw:
                    b_num = re.sub(r"[^\d]", "", b_raw)
                    n3 = f".{b_num.zfill(2)}" if b_num else f".{b_raw}"
                    ruta_texto = f"Soportes / {prod} / {n1} / {n2} / {n3}"
                else:
                    ruta_texto = f"Soportes / {prod} / {n1} / {n2}"
            else:
                ruta_texto = f"Soportes / {prod} / {ver}" if ver else f"Soportes / {prod}"

        auto_str = " (Detectado automáticamente)" if "Auto" in prod_forzado else " (Selección manual)"
        self.lbl_destino_drive.configure(
            text=f"☁️ Destino en Google Drive: {ruta_texto}{auto_str}\n💡 Si alguna carpeta no existe en tu Drive, se creará automáticamente.",
            fg="#38bdf8"
        )

    def iniciar_preanalisis_ia(self, ruta: str):
        if not ruta or not os.path.isfile(ruta):
            return
        if getattr(self, "preanalisis_en_curso", False):
            return

        prov = self.proveedor_ia.get()
        api_key_alt = self.groq_api_key_var.get().strip()

        if prov == "deepseek_groq" and not api_key_alt:
            self.lbl_estado_ia.configure(
                text="💡 Ingresa tu clave gratuita de Groq arriba para activar el autocompletado con DeepSeek R1.",
                fg=TEXT_MUTED
            )
            return

        self.preanalisis_en_curso = True
        if prov == "deepseek_groq":
            self.lbl_estado_ia.configure(
                text="🔍 Analizando pantalla con DeepSeek R1 / Groq para autocompletar versión, dispositivo, severidad...",
                fg="#c084fc"
            )
            self.log_mensaje("\n🔍 [Pre-análisis IA] Examinando evidencia visual con DeepSeek R1 / Groq...")
        else:
            self.lbl_estado_ia.configure(
                text="🔍 Analizando pantalla con Gemini 3.8 para autocompletar versión, dispositivo, severidad y etiquetas...",
                fg="#38bdf8"
            )
            self.log_mensaje("\n🔍 [Pre-análisis IA] Examinando evidencia visual con Gemini 3.8...")

        def _worker():
            try:
                prov_actual = self.proveedor_ia.get()
                modelo_nombre = self.modelo_seleccionado.get()
                modelo_id = OPCIONES_MODELOS_GEMINI.get(modelo_nombre, "gemini-3.6-flash")
                mod_groq_nombre = self.modelo_groq_nombre.get()
                mod_groq_id = MODELOS_GROQ_DISPONIBLES.get(mod_groq_nombre, "openai/gpt-oss-120b")
                key_alt = self.groq_api_key_var.get().strip()

                datos = core.analizar_evidencia_previa(
                    archivo_path=ruta,
                    modelo_gemini=modelo_id,
                    proveedor_ia=prov_actual,
                    modelo_alternativo=mod_groq_id,
                    api_key_alternativa=key_alt
                )
                if datos and any(datos.values()):
                    self.root.after(0, lambda: self._aplicar_datos_preanalisis(datos))
                else:
                    self.root.after(0, lambda: self.lbl_estado_ia.configure(
                        text="💡 Evidencia cargada. Puedes ajustar las opciones manualmente.",
                        fg=TEXT_MUTED
                    ))
            except Exception as e:
                self.root.after(0, lambda: self.lbl_estado_ia.configure(
                    text="💡 Evidencia cargada.",
                    fg=TEXT_MUTED
                ))
            finally:
                self.preanalisis_en_curso = False
                self.root.after(0, self.actualizar_indicador_cuota)

        threading.Thread(target=_worker, daemon=True).start()

    def _aplicar_datos_preanalisis(self, datos: dict):
        version = datos.get("version_app")
        dispositivo = datos.get("dispositivo")
        prioridad = datos.get("prioridad")
        etiquetas = datos.get("etiquetas", [])
        resumen = datos.get("resumen_problema", "")

        cambios = []
        if version:
            self.version_app.set(version)
            self.agregar_version_si_no_existe(version)
            cambios.append(f"Versión: {version}")
        if dispositivo:
            self.dispositivo.set(dispositivo)
            cambios.append(f"Dispositivo: {dispositivo}")
        if prioridad in [1, 2, 3, 4]:
            self.prioridad.set(prioridad)
            p_nombres = {1: "P1 Urgente", 2: "P2 Alta", 3: "P3 Media", 4: "P4 Baja"}
            cambios.append(f"Prioridad: {p_nombres.get(prioridad)}")
        if etiquetas and isinstance(etiquetas, list):
            for tag, var in self.dict_etiquetas.items():
                var.set(tag in etiquetas)
            cambios.append(f"Etiquetas: {', '.join(etiquetas)}")

        desc_actual = self.txt_descripcion.get("1.0", tk.END).strip()
        ruta_arch = (self.archivo_seleccionado.get() or "").lower()
        es_video = ruta_arch.endswith(".mp4") or ruta_arch.endswith(".mov")
        prov_actual = self.proveedor_ia.get()

        # NUNCA autocompletar descripciones si es video y el proveedor es Groq (evita alucinaciones)
        if not desc_actual and resumen and not (prov_actual == "deepseek_groq" and es_video):
            self.txt_descripcion.delete("1.0", tk.END)
            self.txt_descripcion.insert("1.0", resumen)
            cambios.append("Descripción sugerida")

        self.actualizar_preview_destino_drive()

        if prov_actual == "deepseek_groq" and es_video:
            self.lbl_estado_ia.configure(
                text=f"⚡ Video detectado: Se activará el Modo Híbrido (Visión IA + Razonamiento Groq GPT-OSS 120B).",
                fg="#c084fc"
            )
        else:
            self.lbl_estado_ia.configure(
                text=f"✨ ¡IA Autocompletó los campos según la evidencia! ({', '.join(cambios[:3])}). Puedes modificarlos si deseas.",
                fg=ACCENT_GREEN
            )
        self.log_mensaje(f"✨ [Pre-análisis IA] Autocompletado con éxito: {', '.join(cambios)}")

    def actualizar_archivo_seleccionado(self, ruta: str):
        self.archivo_seleccionado.set(ruta)
        nombre = os.path.basename(ruta)
        ext = os.path.splitext(ruta)[1].lower()
        tamano_mb = os.path.getsize(ruta) / (1024 * 1024) if os.path.exists(ruta) else 0

        tipo_icono = "📹" if ext == ".mp4" else "🖼️"
        self.lbl_drop_icon.configure(
            text=f"{tipo_icono}  {nombre} ({tamano_mb:.1f} MB)",
            fg=ACCENT_GREEN
        )
        self.lbl_drop_sub.configure(
            text=f"Ruta: {ruta}  •  (Arrastra otro archivo o haz clic para cambiar)",
            fg=TEXT_MAIN
        )
        # Disparar pre-análisis inteligente de la evidencia
        self.iniciar_preanalisis_ia(ruta)

    def examinar_archivo(self):
        tipos = [
            ("Archivos de evidencia", "*.mp4;*.png;*.jpg;*.jpeg"),
            ("Videos MP4", "*.mp4"),
            ("Imágenes", "*.png;*.jpg;*.jpeg"),
            ("Todos los archivos", "*.*")
        ]
        ruta = filedialog.askopenfilename(title="Seleccionar evidencia de QA", filetypes=tipos)
        if ruta:
            self.actualizar_archivo_seleccionado(ruta)

    def cargar_evidencias_recientes(self):
        directorios = [
            getattr(core, "VIDEOS_DEFAULT_DIR", r"C:\Users\Administrador\Desktop\Reportes QA"),
            os.path.join(os.path.expanduser("~"), "Desktop")
        ]
        archivos = []
        valid_exts = getattr(core, "EXTENSIONES_VALIDAS", [".mp4", ".png", ".jpg", ".jpeg"])
        for d in directorios:
            if os.path.exists(d):
                for f in os.listdir(d):
                    ext = os.path.splitext(f)[1].lower()
                    if ext in valid_exts:
                        full = os.path.join(d, f)
                        if os.path.isfile(full):
                            archivos.append((os.path.getmtime(full), full))

        archivos.sort(key=lambda x: x[0], reverse=True)
        recientes = [a[1] for a in archivos[:8]]

        if recientes:
            self.combo_recientes["values"] = recientes
            self.combo_recientes.set("Seleccionar de la lista de evidencias recientes...")
        else:
            self.combo_recientes["values"] = ["No se encontraron evidencias recientes"]
            self.combo_recientes.set("No se encontraron evidencias recientes")

    def on_reciente_seleccionado(self, event):
        val = self.combo_recientes.get()
        if os.path.isfile(val):
            self.actualizar_archivo_seleccionado(val)

    def log_mensaje(self, mensaje: str):
        self.txt_logs.configure(state="normal")
        self.txt_logs.insert(tk.END, f"{mensaje}\n")
        self.txt_logs.see(tk.END)
        self.txt_logs.configure(state="disabled")

    def actualizar_paso(self, clave: str, estado: str, texto_estado: str, color_borde: str):
        def _update():
            p = self.pasos_ui.get(clave)
            if p:
                p["frame"].configure(highlightbackground=color_borde, highlightthickness=1)
                p["estado"].configure(text=f"{estado} {texto_estado}", fg=color_borde)
        self.root.after(0, _update)

    def iniciar_proceso(self):
        if self.proceso_en_curso:
            return

        ruta = self.archivo_seleccionado.get().strip().strip('"')
        if not ruta or not os.path.isfile(ruta):
            messagebox.showerror("Error", "Por favor selecciona o arrastra un archivo de evidencia válido (.mp4, .png, .jpg).")
            return

        ext = os.path.splitext(ruta)[1].lower()
        descripcion = self.txt_descripcion.get("1.0", tk.END).strip()

        if ext in [".png", ".jpg", ".jpeg"] and not descripcion:
            messagebox.showwarning(
                "Descripción Requerida",
                "Para capturas de imagen es obligatorio exponer textualmente qué ocurrió o qué botón falló."
            )
            self.txt_descripcion.focus_set()
            return

        # Etiquetas seleccionadas
        etiquetas = [tag for tag, var in self.dict_etiquetas.items() if var.get()]
        if not etiquetas:
            etiquetas = ["Bug"]

        # Preparar UI para ejecución
        self.proceso_en_curso = True
        self.btn_generar.configure(state="disabled", text="⏳  Procesando Reporte...", bg=BG_CARD_LIGHT)
        self.btn_cancelar.configure(text="⛔  Cancelar (ESC)")
        self.progreso["value"] = 5

        # Resetear estados de pasos
        for clave in self.pasos_ui:
            self.actualizar_paso(clave, "⚪", "Pendiente", BORDER_COLOR)

        # Iniciar hilo de procesamiento en segundo plano
        prov = self.proveedor_ia.get()
        if prov == "deepseek_groq" and not self.groq_api_key_var.get().strip():
            messagebox.showwarning(
                "Clave de Groq Requerida",
                "Para utilizar DeepSeek R1 / Groq de forma gratuita necesitas tu API Key de Groq.\n\n"
                "1. Haz clic en el enlace '🔑 Obtener gratis sin tarjeta en console.groq.com'\n"
                "2. Copia tu clave (empieza con gsk_...)\n"
                "3. Pégala en el campo correspondiente y pulsa '💾 Guardar Clave'."
            )
            self.entry_groq_key.focus_set()
            return

        modelo_nombre = self.modelo_seleccionado.get()
        modelo_id = OPCIONES_MODELOS_GEMINI.get(modelo_nombre, "gemini-3.6-flash")
        mod_groq_nombre = self.modelo_groq_nombre.get()
        mod_groq_id = MODELOS_GROQ_DISPONIBLES.get(mod_groq_nombre, "openai/gpt-oss-120b")
        api_key_alt = self.groq_api_key_var.get().strip()

        self.hilo_trabajo = threading.Thread(
            target=self._ejecutar_worker,
            args=(
                ruta,
                self.version_app.get(),
                self.dispositivo.get(),
                self.prioridad.get(),
                etiquetas,
                descripcion,
                modelo_id,
                self.razonamiento_ampliado.get(),
                self.modulo_producto.get(),
                prov,
                mod_groq_id,
                api_key_alt
            ),
            daemon=True
        )
        self.hilo_trabajo.start()

    def _ejecutar_worker(self, ruta, version, dispositivo, prioridad, etiquetas, descripcion,
                         modelo_gemini, razonamiento_ampliado, modulo_producto,
                         proveedor_ia="gemini", modelo_alternativo="openai/gpt-oss-120b",
                         api_key_alternativa=""):
        try:
            if proveedor_ia == "deepseek_groq":
                self.log_mensaje(f"🚀 Iniciando automatización de QA (Motor: Groq IA - {modelo_alternativo})...")
            else:
                modo_thinking_str = " + Razonamiento Ampliado" if razonamiento_ampliado else ""
                self.log_mensaje(f"🚀 Iniciando automatización de QA (Motor: {modelo_gemini}{modo_thinking_str})...")
            self.progreso["value"] = 15

            # Conectar hook de cambio de pasos en vivo
            core.hook_cambio_paso = self.actualizar_paso

            # Hook para reflejar el conteo regresivo de reintentos en la interfaz
            def on_conteo_espera(restante):
                if restante > 0:
                    self.actualizar_paso("gemini_analysis", "⏳", f"Reintento en {restante}s", ACCENT_YELLOW)
                    self.btn_cancelar.configure(text=f"⛔  Cancelar ({restante}s) [ESC]")
                else:
                    self.actualizar_paso("gemini_analysis", "⏳", "Reintentando...", ACCENT_BLUE)
                    self.btn_cancelar.configure(text="⛔  Cancelar (ESC)")

            core.hook_conteo_espera = on_conteo_espera

            # Ejecutar procesamiento core
            resultado = core.procesar_evidencia(
                archivo_path=ruta,
                version_app=version,
                dispositivo=dispositivo,
                prioridad_ui=prioridad,
                etiquetas=etiquetas,
                renombrar_local=True,
                descripcion_problema=descripcion,
                modelo_gemini=modelo_gemini,
                razonamiento_ampliado=razonamiento_ampliado,
                modulo_producto=modulo_producto,
                proveedor_ia=proveedor_ia,
                modelo_alternativo=modelo_alternativo,
                api_key_alternativa=api_key_alternativa
            )

            self.progreso["value"] = 100
            self.actualizar_paso("gemini_upload", "✅", "Listo", ACCENT_GREEN)
            self.actualizar_paso("gemini_analysis", "✅", "Listo", ACCENT_GREEN)
            self.actualizar_paso("drive_upload", "✅", "Listo", ACCENT_GREEN)
            self.actualizar_paso("todoist_create", "✅", "Publicado", ACCENT_GREEN)

            self.log_mensaje("\n✨ ¡PROCESO COMPLETADO SATISFACTORIAMENTE!")
            if resultado and isinstance(resultado, dict):
                self.log_mensaje(f"📋 Formato para WhatsApp copiado al portapapeles:\n{resultado.get('mensaje_whatsapp', '')}")
                self.root.after(0, lambda: DialogoExitoWhatsApp(self.root, resultado, on_revertir=self.revertir_reporte_desde_gui))
            else:
                self.log_mensaje("⚠️ Proceso concluido sin datos de retorno.")

            # Limpiar formulario automáticamente 4 segundos después del éxito
            self.root.after(4000, self._limpiar_formulario_completo)

        except core.ProcesoCanceladoException:
            self.log_mensaje("\n⛔ Proceso cancelado o revertido limpiamente.")
            for clave in ["gemini_upload", "gemini_analysis", "drive_upload", "todoist_create"]:
                p = self.pasos_ui.get(clave)
                if p and "Listo" not in p["estado"].cget("text") and "Publicado" not in p["estado"].cget("text"):
                    self.actualizar_paso(clave, "⚪", "Cancelado", BORDER_COLOR)
            messagebox.showinfo("Cancelado", "El proceso fue cancelado / revertido limpiamente.")
        except Exception as e:
            import traceback
            traceback.print_exc()
            info_error = traducir_error_a_humano(e)
            self.log_mensaje(f"\n❌ {info_error['titulo']}:\n{info_error['mensaje']}\n(Detalle técnico: {str(e)[:160]})")
            for clave in ["gemini_upload", "gemini_analysis", "drive_upload", "todoist_create"]:
                p = self.pasos_ui.get(clave)
                if p and "Listo" not in p["estado"].cget("text") and "Publicado" not in p["estado"].cget("text"):
                    self.actualizar_paso(clave, "❌", "Error", ACCENT_RED)
            messagebox.showwarning(info_error["titulo"], info_error["mensaje"])
        finally:
            core.hook_cambio_paso = None
            core.hook_conteo_espera = None
            self.proceso_en_curso = False
            self.btn_generar.configure(state="normal", text="🚀  Generar y Subir Reporte a Todoist", bg=ACCENT_BLUE)
            self.btn_cancelar.configure(state="normal", text="⛔  Cancelar (ESC)", bg=ACCENT_RED)
            # Actualizar cuota inmediatamente y 3 segundos después para reflejar el uso real
            self.root.after(0, self.actualizar_indicador_cuota)
            self.root.after(3000, self.actualizar_indicador_cuota)

    def cancelar_proceso(self):
        if self.proceso_en_curso:
            if core._cancelacion_solicitada:
                if messagebox.askyesno("Forzar Cierre", "¿El proceso está tardando en responder a la cancelación? ¿Deseas cerrar la ventana de inmediato?", parent=self.root):
                    self.cerrar_ventana()
                return
            core._cancelacion_solicitada = True
            self.log_mensaje("\n⛔ Solicitud de cancelación enviada. Abortando proceso...")
            self.btn_cancelar.configure(text="⏳ Cancelando (clic para forzar)...", bg=ACCENT_YELLOW)
        else:
            self.cerrar_ventana()

    def revertir_reporte_desde_gui(self, resultado: dict):
        if not resultado:
            return
        self.log_mensaje("\n🔄 Revertiendo reporte por solicitud del usuario...")
        try:
            core.revertir_acciones(
                ruta_original=resultado.get("ruta_original"),
                ruta_renombrada=resultado.get("archivo_final"),
                drive_service=resultado.get("drive_service"),
                drive_file_id=resultado.get("drive_file_id"),
                task_id=resultado.get("tarea_id")
            )
            self.log_mensaje("✅ Reversión completada con éxito.")
            messagebox.showinfo("Reversión Completada", "El reporte en Todoist y Google Drive ha sido revertido limpiamente.")
        except Exception as e:
            self.log_mensaje(f"⚠️ Error durante la reversión: {e}")
            messagebox.showwarning("Aviso de Reversión", f"Ocurrió un detalle al revertir: {e}")

    def cerrar_ventana(self):
        try:
            sys.stdout = self.old_stdout
            sys.stderr = self.old_stderr
        except Exception:
            pass
        self.root.destroy()


def abrir_modal_gui():
    root = tk.Tk()
    app = ModalSubirReporteQA(root)
    root.mainloop()


if __name__ == "__main__":
    abrir_modal_gui()
