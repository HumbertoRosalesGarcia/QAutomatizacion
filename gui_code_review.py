"""
gui_code_review.py
=============================================================================
Modal / Interfaz Gráfica para el Sistema Inteligente de Code Review / QA
Permite seleccionar dispositivos ADB conectados, listar y elegir tareas
de la columna "Code Review / QA" en Todoist, y monitorear en vivo la
verificación autónoma sobre el dispositivo físico o emulador.
=============================================================================
"""

import os
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox


from code_review import (
    ADBController,
    TodoistReviewClient,
    BugReportParser,
    ejecutar_verificacion_en_dispositivo,
    ejecutar_verificacion_por_video_y_codigo,
    ProcesoCanceladoException,
    APPIUM_SERVER_URL,
    DEFAULT_PACKAGE
)


class VentanaCodeReviewQA(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🤖 Code Review / QA - Verificación Inteligente de Bugs")
        self.geometry("880x820")
        self.minsize(820, 720)

        self.COLOR_BG = "#181825"
        self.COLOR_CARD = "#1e1e2e"
        self.COLOR_CARD_BORDER = "#313244"
        self.COLOR_TEXT = "#cdd6f4"
        self.COLOR_SUBTEXT = "#a6adc8"
        self.COLOR_PRIMARY = "#89b4fa"
        self.COLOR_SUCCESS = "#a6e3a1"
        self.COLOR_WARNING = "#f9e2af"
        self.COLOR_DANGER = "#f38ba8"
        self.COLOR_INPUT_BG = "#313244"

        self.configure(bg=self.COLOR_BG)

        self.var_dispositivo_sel = tk.StringVar()
        self.var_appium_url = tk.StringVar(value=APPIUM_SERVER_URL)
        self.var_tarea_url = tk.StringVar()
        self.var_proveedor_ia = tk.StringVar(value="Groq Cloud (OpenAI GPT-OSS 120B / Qwen)")
        self.var_modo_ejecucion = tk.StringVar(value="📱 Dispositivo Físico (+ Fallback a Video)")
        self.var_estado_cuota = tk.StringVar(value="Consultando cuota de IA...")

        self.dispositivos_lista = []
        self.tareas_todoist_lista = []
        self.proceso_en_curso = False
        self.adb_actual = None

        self._gui_queue = queue.Queue()
        self._check_gui_queue()

        self._configurar_estilos()
        self._construir_interfaz()
        self._cargar_dispositivos()
        self._actualizar_cuota_async()

        self.bind("<Escape>", lambda e: self.solicitar_cancelacion())

    def _check_gui_queue(self):
        try:
            while True:
                fn = self._gui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(50, self._check_gui_queue)

    def ejecutar_en_ui(self, fn):
        self._gui_queue.put(fn)



    def _configurar_estilos(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TLabel", background=self.COLOR_CARD, foreground=self.COLOR_TEXT, font=("Segoe UI", 10))
        style.configure("Header.TLabel", background=self.COLOR_BG, foreground=self.COLOR_PRIMARY, font=("Segoe UI", 16, "bold"))
        style.configure("Subheader.TLabel", background=self.COLOR_BG, foreground=self.COLOR_SUBTEXT, font=("Segoe UI", 9))
        style.configure("TProgressbar", thickness=10)

        # Configuración de alto contraste y legibilidad para Comboboxes
        style.configure(
            "TCombobox",
            fieldbackground="#212234",
            background="#313244",
            foreground="#ffffff",
            darkcolor="#313244",
            lightcolor="#313244",
            bordercolor="#45475a",
            arrowcolor="#89b4fa",
            arrowsize=14,
            padding=4
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#212234"), ("disabled", "#181825"), ("active", "#28293d")],
            foreground=[("readonly", "#ffffff"), ("disabled", "#6c7086"), ("active", "#ffffff")],
            selectbackground=[("readonly", "#313244"), ("active", "#313244")],
            selectforeground=[("readonly", "#ffffff"), ("active", "#ffffff")],
            background=[("readonly", "#313244"), ("active", "#45475a")]
        )

        # Estilo para el menú desplegable (Listbox) de los Comboboxes
        self.option_add("*TCombobox*Listbox.background", "#212234")
        self.option_add("*TCombobox*Listbox.foreground", "#ffffff")
        self.option_add("*TCombobox*Listbox.selectBackground", "#89b4fa")
        self.option_add("*TCombobox*Listbox.selectForeground", "#11111b")
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 9))

    def _construir_interfaz(self):
        main_frame = tk.Frame(self, bg=self.COLOR_BG)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=14)

        header_frame = tk.Frame(main_frame, bg=self.COLOR_BG)
        header_frame.pack(fill=tk.X, pady=(0, 10))

        lbl_titulo = ttk.Label(header_frame, text="🤖 Sistema Inteligente de Code Review / QA", style="Header.TLabel")
        lbl_titulo.pack(anchor=tk.W)

        lbl_sub = ttk.Label(header_frame, text="Automatización con Appium + ADB + Groq 120B / Gemini IA sobre dispositivos Android", style="Subheader.TLabel")
        lbl_sub.pack(anchor=tk.W)

        # Card ADB
        card_adb = tk.LabelFrame(
            main_frame,
            text=" 📱 Dispositivo Android (ADB) & Appium Server ",
            bg=self.COLOR_CARD,
            fg=self.COLOR_PRIMARY,
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            highlightbackground=self.COLOR_CARD_BORDER,
            highlightthickness=1
        )
        card_adb.pack(fill=tk.X, pady=(0, 8), padx=2, ipady=4)

        f_dev = tk.Frame(card_adb, bg=self.COLOR_CARD)
        f_dev.pack(fill=tk.X, padx=10, pady=(4, 4))

        tk.Label(f_dev, text="Dispositivo ADB:", bg=self.COLOR_CARD, fg=self.COLOR_SUBTEXT, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.combo_dispositivos = ttk.Combobox(f_dev, textvariable=self.var_dispositivo_sel, state="readonly", width=42)
        self.combo_dispositivos.pack(side=tk.LEFT, padx=(8, 8))

        btn_refrescar_adb = tk.Button(
            f_dev,
            text="🔄 Refrescar ADB",
            bg=self.COLOR_PRIMARY,
            fg="#11111b",
            font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT,
            cursor="hand2",
            command=self._cargar_dispositivos
        )
        btn_refrescar_adb.pack(side=tk.LEFT)

        tk.Label(f_dev, text="Appium URL:", bg=self.COLOR_CARD, fg=self.COLOR_SUBTEXT, font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(16, 4))
        entry_appium = tk.Entry(
            f_dev,
            textvariable=self.var_appium_url,
            bg=self.COLOR_INPUT_BG,
            fg="white",
            insertbackground="white",
            font=("Segoe UI", 9),
            width=22,
            relief=tk.FLAT
        )
        entry_appium.pack(side=tk.LEFT)

        # Card IA & Cuotas
        card_ia = tk.LabelFrame(
            main_frame,
            text=" 🧠 Motor de Inteligencia Artificial & Cuota de Créditos ",
            bg=self.COLOR_CARD,
            fg=self.COLOR_PRIMARY,
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            highlightbackground=self.COLOR_CARD_BORDER,
            highlightthickness=1
        )
        card_ia.pack(fill=tk.X, pady=(0, 8), padx=2, ipady=4)

        f_ia_sel = tk.Frame(card_ia, bg=self.COLOR_CARD)
        f_ia_sel.pack(fill=tk.X, padx=10, pady=(4, 2))

        tk.Label(f_ia_sel, text="Motor IA:", bg=self.COLOR_CARD, fg=self.COLOR_SUBTEXT, font=("Segoe UI", 9)).pack(side=tk.LEFT)

        self.combo_ia = ttk.Combobox(
            f_ia_sel,
            textvariable=self.var_proveedor_ia,
            state="readonly",
            width=36,
            values=[
                "Groq Cloud (OpenAI GPT-OSS 120B / Qwen)",
                "Google Gemini (Gemini 3.8 Flash / 3.6 Flash)"
            ]
        )
        self.combo_ia.current(0)
        self.combo_ia.pack(side=tk.LEFT, padx=(6, 10))
        self.combo_ia.bind("<<ComboboxSelected>>", self._on_cambio_proveedor_ia)

        tk.Label(f_ia_sel, text="Modo:", bg=self.COLOR_CARD, fg=self.COLOR_SUBTEXT, font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(6, 2))

        self.combo_modo = ttk.Combobox(
            f_ia_sel,
            textvariable=self.var_modo_ejecucion,
            state="readonly",
            width=36,
            values=[
                "📱 Dispositivo Físico (+ Fallback a Video)",
                "🎥 Plan B: Solo Video + Código (Sin Dispositivo)"
            ]
        )
        self.combo_modo.current(0)
        self.combo_modo.pack(side=tk.LEFT, padx=(4, 8))

        # Fila de Cuota / Créditos
        f_cuota = tk.Frame(card_ia, bg=self.COLOR_CARD)
        f_cuota.pack(fill=tk.X, padx=10, pady=(4, 4))

        self.lbl_cuota_badge = tk.Label(
            f_cuota,
            textvariable=self.var_estado_cuota,
            bg=self.COLOR_INPUT_BG,
            fg="#a6e3a1",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=3,
            relief=tk.FLAT
        )
        self.lbl_cuota_badge.pack(side=tk.LEFT, fill=tk.X, expand=True)

        btn_refrescar_cuota = tk.Button(
            f_cuota,
            text="🔄 Actualizar Cuota",
            bg=self.COLOR_PRIMARY,
            fg="#11111b",
            font=("Segoe UI", 8, "bold"),
            relief=tk.FLAT,
            cursor="hand2",
            command=lambda: self._actualizar_cuota_async(forzar=True)
        )
        btn_refrescar_cuota.pack(side=tk.LEFT, padx=(8, 0))

        # Card Todoist
        card_todoist = tk.LabelFrame(
            main_frame,
            text=" 📋 Tarea a Verificar ",
            bg=self.COLOR_CARD,
            fg=self.COLOR_PRIMARY,
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            highlightbackground=self.COLOR_CARD_BORDER,
            highlightthickness=1
        )
        card_todoist.pack(fill=tk.X, pady=(0, 8), padx=2, ipady=4)

        f_td_manual = tk.Frame(card_todoist, bg=self.COLOR_CARD)
        f_td_manual.pack(fill=tk.X, padx=10, pady=(6, 6))

        tk.Label(f_td_manual, text="Introduce el enlace de Todoist:", bg=self.COLOR_CARD, fg=self.COLOR_SUBTEXT, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        entry_url = tk.Entry(
            f_td_manual,
            textvariable=self.var_tarea_url,
            bg=self.COLOR_INPUT_BG,
            fg="white",
            insertbackground="white",
            font=("Segoe UI", 9),
            relief=tk.FLAT
        )
        entry_url.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0), ipady=3)

        # Botones
        f_botones = tk.Frame(main_frame, bg=self.COLOR_BG)
        f_botones.pack(fill=tk.X, pady=(2, 8))

        self.btn_iniciar = tk.Button(
            f_botones,
            text="🚀 Iniciar Verificación Autónoma en Dispositivo",
            bg=self.COLOR_PRIMARY,
            fg="#11111b",
            font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT,
            cursor="hand2",
            command=self.iniciar_verificacion
        )
        self.btn_iniciar.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)

        self.btn_cancelar = tk.Button(
            f_botones,
            text="🛑 Detener / Cancelar (ESC)",
            bg=self.COLOR_DANGER,
            fg="#11111b",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            cursor="hand2",
            command=self.solicitar_cancelacion
        )
        self.btn_cancelar.pack(side=tk.RIGHT, padx=(10, 0), ipady=6, ipadx=10)

        # Card Log y Veredicto
        card_log = tk.LabelFrame(
            main_frame,
            text=" 📊 Progreso de Verificación & Logs en Vivo ",
            bg=self.COLOR_CARD,
            fg=self.COLOR_PRIMARY,
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            highlightbackground=self.COLOR_CARD_BORDER,
            highlightthickness=1
        )
        card_log.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2), ipady=4)

        self.lbl_veredicto = tk.Label(
            card_log,
            text="Esperando inicio de prueba...",
            bg=self.COLOR_CARD,
            fg=self.COLOR_SUBTEXT,
            font=("Segoe UI", 11, "bold")
        )
        self.lbl_veredicto.pack(fill=tk.X, padx=10, pady=(2, 4))

        f_text = tk.Frame(card_log, bg=self.COLOR_CARD)
        f_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        self.txt_log = tk.Text(
            f_text,
            height=12,
            bg="#11111b",
            fg="#a6e3a1",
            font=("Consolas", 9),
            relief=tk.FLAT
        )
        scroll_log = ttk.Scrollbar(f_text, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll_log.set)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_log.pack(side=tk.RIGHT, fill=tk.Y)

    def _actualizar_cuota_async(self, forzar: bool = False):
        """Consulta el estado de cuota y tiempo de reinicio en segundo plano sin bloquear la UI."""
        prov_text = self.var_proveedor_ia.get().lower()
        prov_key = "groq" if "groq" in prov_text else "gemini"

        def _worker(pk):
            try:
                from ai_quota_manager import consultar_estado_cuotas
                info = consultar_estado_cuotas(proveedor=pk, forzar=forzar)
                resumen = info.get("resumen_texto", "")
                color = "#a6e3a1" if info.get("estado") == "activo" else "#f9e2af"
                self.ejecutar_en_ui(lambda: self._set_cuota_ui(resumen, color))
            except Exception as e:
                self.ejecutar_en_ui(lambda: self._set_cuota_ui(f"⚠️ Error al consultar cuota: {e}", "#f38ba8"))

        threading.Thread(target=_worker, args=(prov_key,), daemon=True).start()

    def _set_cuota_ui(self, texto, color):
        self.var_estado_cuota.set(texto)
        self.lbl_cuota_badge.config(fg=color)

    def _on_cambio_proveedor_ia(self, event=None):
        self.var_estado_cuota.set("Consultando cuota actualizada...")
        self._actualizar_cuota_async(forzar=True)

    def _cargar_dispositivos(self):
        self.dispositivos_lista = ADBController.listar_dispositivos()
        opciones = []
        for d in self.dispositivos_lista:
            opciones.append(f"{d['fabricante']} {d['modelo']} ({d['serial']}) - [{d['estado']}]")
        self.combo_dispositivos["values"] = opciones
        if opciones:
            self.combo_dispositivos.current(0)
        else:
            self.var_dispositivo_sel.set("Ningún dispositivo ADB detectado")

    def log(self, mensaje):
        def _escribir():
            self.txt_log.insert(tk.END, f"{mensaje}\n")
            self.txt_log.see(tk.END)
        self.ejecutar_en_ui(_escribir)

    def set_veredicto(self, texto, tipo="info"):
        colores = {
            "info": self.COLOR_PRIMARY,
            "corregido": self.COLOR_SUCCESS,
            "mantiene": self.COLOR_DANGER,
            "espera": self.COLOR_WARNING,
        }
        col = colores.get(tipo, self.COLOR_TEXT)
        self.ejecutar_en_ui(lambda: self.lbl_veredicto.config(text=texto, fg=col))


    def iniciar_verificacion(self):
        es_modo_solo_video = "Solo Video" in self.var_modo_ejecucion.get()
        idx = self.combo_dispositivos.current()
        serial = None

        if not es_modo_solo_video:
            if idx < 0 or idx >= len(self.dispositivos_lista):
                resp = messagebox.askyesno(
                    "Sin Dispositivo ADB",
                    "No se detectó ningún dispositivo Android conectado.\n\n"
                    "¿Deseas ejecutar la verificación en Modo Plan B (Auditoría por Video de Evidencia + Código On The Fly)?",
                    parent=self
                )
                if resp:
                    self.var_modo_ejecucion.set("🎥 Plan B: Solo Video + Código (Sin Dispositivo)")
                    es_modo_solo_video = True
                else:
                    return
            else:
                serial = self.dispositivos_lista[idx]["serial"]

        tarea_ref = self.var_tarea_url.get().strip()
        if not tarea_ref:
            messagebox.showwarning("Falta Tarea", "Por favor introduce el enlace de la tarea en Todoist.", parent=self)
            return

        import code_review
        code_review.CANCELAR_PROCESO = False

        self.proceso_en_curso = True
        self.btn_iniciar.config(state=tk.DISABLED, bg="#45475a")
        self.txt_log.delete("1.0", tk.END)

        if es_modo_solo_video:
            self.set_veredicto("⏳ Iniciando Plan B (Auditoría por Video + Código)...", "espera")
        else:
            self.set_veredicto("⏳ Inicializando Appium y preparando entorno...", "espera")

        threading.Thread(target=self._ejecutar_en_hilo, args=(serial, tarea_ref, es_modo_solo_video), daemon=True).start()

    def _ejecutar_en_hilo(self, serial, tarea_ref, es_modo_solo_video):
        prov_text = self.var_proveedor_ia.get().lower()
        proveedor_ia = "groq" if "groq" in prov_text else "gemini"

        try:
            self.log(f"🤖 Motor de Inteligencia Artificial seleccionado: {proveedor_ia.upper()}")
            self.log("📋 Obteniendo información de la tarea en Todoist...")
            td = TodoistReviewClient()
            t_id = td.extraer_id_desde_url(tarea_ref)
            t_data = td.obtener_tarea_por_id(t_id)
            comentarios = td.obtener_comentarios(t_id)
            reporte = BugReportParser.parsear(t_data, comentarios)

            titulo_tarea = reporte.get("titulo", "Tarea Todoist")
            self.log(f"📌 Tarea a verificar: {titulo_tarea}")

            if es_modo_solo_video:
                self.log("🎥 Modo Activo: Plan B (Auditoría Cognitiva de Video + Estructura de Código)")
                self.set_veredicto(f"🎥 Auditando Video + Código: {titulo_tarea[:50]}...", "info")
                veredicto = ejecutar_verificacion_por_video_y_codigo(
                    reporte=reporte,
                    proveedor_ia=proveedor_ia,
                    motivo_activacion="Modo Plan B: Auditoría por Video + Código"
                )
                tipo = "corregido" if veredicto.get("veredicto") == "CORREGIDO" else "mantiene"
                self.set_veredicto(f"⚖️ {veredicto.get('mensaje_principal', 'Dictamen emitido')}", tipo)
            else:
                self.log(f"🚀 Conectando con dispositivo ADB [{serial}]...")
                self.adb_actual = ADBController(serial=serial, appium_url=self.var_appium_url.get().strip())
                self.adb_actual.activar_indicadores_visuales()
                self.adb_actual.conectar_appium()

                self.set_veredicto(f"🤖 Verificando en dispositivo: {titulo_tarea[:50]}...", "info")
                try:
                    ejecutar_verificacion_en_dispositivo(
                        adb=self.adb_actual,
                        reporte=reporte,
                        proveedor_ia=proveedor_ia,
                        permitir_fallback_video=True
                    )
                    self.set_veredicto("✅ Verificación completada con éxito.", "corregido")
                except ProcesoCanceladoException as e_can:
                    raise e_can
                except Exception as e_disp:
                    self.log(f"\n⚠️ Interrupción en prueba física: {e_disp}")
                    self.log("🎥 Activando Fallback Automático: Plan B (Auditoría por Video + Código On The Fly)...")
                    veredicto = ejecutar_verificacion_por_video_y_codigo(
                        reporte=reporte,
                        proveedor_ia=proveedor_ia,
                        motivo_activacion=f"Fallo en dispositivo físico: {e_disp}"
                    )
                    tipo = "corregido" if veredicto.get("veredicto") == "CORREGIDO" else "mantiene"
                    self.set_veredicto(f"⚖️ {veredicto.get('mensaje_principal', 'Dictamen emitido')}", tipo)

        except ProcesoCanceladoException as e:
            self.log(f"\n🛑 {e}")
            self.set_veredicto("🛑 Verificación cancelada por el usuario.", "mantiene")
        except Exception as e:
            self.log(f"\n❌ Error durante la verificación: {e}")
            self.set_veredicto(f"❌ Error: {e}", "mantiene")
        finally:
            self.proceso_en_curso = False
            import code_review
            code_review.CANCELAR_PROCESO = False
            if self.adb_actual:
                try:
                    self.adb_actual.cerrar_sesion()
                except Exception:
                    pass
                self.adb_actual = None
            self.ejecutar_en_ui(lambda: self.btn_iniciar.config(state=tk.NORMAL, bg=self.COLOR_PRIMARY))
            self._actualizar_cuota_async(forzar=True)



    def solicitar_cancelacion(self):
        if self.proceso_en_curso:
            self.log("⚠️ Señal de cancelación enviada al proceso en ejecución...")
            import code_review
            code_review.CANCELAR_PROCESO = True
        else:
            self.destroy()


def abrir_modal_code_review():
    app = VentanaCodeReviewQA()
    app.mainloop()


if __name__ == "__main__":
    abrir_modal_code_review()
