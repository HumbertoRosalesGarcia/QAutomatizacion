"""
launcher.py
=============================================================================
Lanzador Central de QA Automation Suite
Permite seleccionar rápidamente entre:
1. Subir Reporte de QA (gui_subir_reporte.py)
2. Verificar Bug Corregido (gui_code_review.py / code_review.py)
=============================================================================
"""

import os
import sys
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

# Colores Dark Modern (Slate / Indigo)
BG_MAIN = "#0f172a"        # Slate 900
BG_CARD = "#1e293b"        # Slate 800
BG_CARD_HOVER = "#334155"  # Slate 700
BORDER_COLOR = "#334155"   # Slate 700
TEXT_MAIN = "#f8fafc"      # Slate 50
TEXT_MUTED = "#94a3b8"     # Slate 400
ACCENT_BLUE = "#3b82f6"    # Blue 500
ACCENT_BLUE_HOVER = "#2563eb"
ACCENT_GREEN = "#10b981"   # Emerald 500
ACCENT_GREEN_HOVER = "#059669"

FONT_TITLE = ("Segoe UI", 16, "bold")
FONT_SUBTITLE = ("Segoe UI", 10)
FONT_CARD_TITLE = ("Segoe UI", 13, "bold")
FONT_CARD_DESC = ("Segoe UI", 9)
FONT_BTN = ("Segoe UI", 10, "bold")
FONT_FOOTER = ("Segoe UI", 8)


class LauncherQA:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("QA Automation Suite - Selector de Flujo")

        # Centrar en pantalla (660 x 420)
        win_w, win_h = 660, 420
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        pos_x = max(0, (screen_w - win_w) // 2)
        pos_y = max(0, (screen_h - win_h - 40) // 2)

        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(620, 390)
        self.root.configure(bg=BG_MAIN)

        self.construir_interfaz()
        self.vincular_eventos()

    def construir_interfaz(self):
        container = tk.Frame(self.root, bg=BG_MAIN, padx=24, pady=20)
        container.pack(fill=tk.BOTH, expand=True)

        # Header
        lbl_titulo = tk.Label(
            container,
            text="🛠️  QA Automation Suite",
            font=FONT_TITLE,
            fg=TEXT_MAIN,
            bg=BG_MAIN
        )
        lbl_titulo.pack(anchor="w")

        lbl_sub = tk.Label(
            container,
            text="Centro de Control • Elige la herramienta que deseas ejecutar hoy:",
            font=FONT_SUBTITLE,
            fg=TEXT_MUTED,
            bg=BG_MAIN
        )
        lbl_sub.pack(anchor="w", pady=(2, 16))

        # Contenedor de Tarjetas (2 columnas simétricas)
        cards_frame = tk.Frame(container, bg=BG_MAIN)
        cards_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 16))
        cards_frame.columnconfigure(0, weight=1, uniform="card")
        cards_frame.columnconfigure(1, weight=1, uniform="card")
        cards_frame.rowconfigure(0, weight=1)

        # ==========================================
        # TARJETA 1: SUBIR REPORTE DE QA
        # ==========================================
        self.card_subir = tk.Frame(
            cards_frame,
            bg=BG_CARD,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=BORDER_COLOR,
            highlightthickness=2,
            padx=16,
            pady=16,
            cursor="hand2"
        )
        self.card_subir.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        lbl_icono_subir = tk.Label(
            self.card_subir,
            text="🚀",
            font=("Segoe UI Emoji", 30),
            bg=BG_CARD,
            fg="#60a5fa"
        )
        lbl_icono_subir.pack(pady=(0, 4))

        lbl_titulo_subir = tk.Label(
            self.card_subir,
            text="Subir Reporte de QA",
            font=FONT_CARD_TITLE,
            fg="#60a5fa",
            bg=BG_CARD
        )
        lbl_titulo_subir.pack(pady=(0, 6))

        desc_subir = (
            "• Pre-análisis inteligente de video/captura con IA\n"
            "• Ruteo automático en Google Drive (Soportes)\n"
            "• Creación automática de tickets en Todoist\n"
            "• Formato listo para pegar en WhatsApp"
        )
        lbl_desc_subir = tk.Label(
            self.card_subir,
            text=desc_subir,
            font=FONT_CARD_DESC,
            fg=TEXT_MUTED,
            bg=BG_CARD,
            justify="left"
        )
        lbl_desc_subir.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.btn_subir = tk.Button(
            self.card_subir,
            text="🚀  Subir Reporte (1)",
            font=FONT_BTN,
            bg=ACCENT_BLUE,
            fg="#ffffff",
            activebackground=ACCENT_BLUE_HOVER,
            activeforeground="#ffffff",
            bd=0,
            pady=8,
            cursor="hand2",
            command=self.abrir_subir_reporte
        )
        self.btn_subir.pack(fill=tk.X)

        # ==========================================
        # TARJETA 2: VERIFICAR BUG CORREGIDO
        # ==========================================
        self.card_review = tk.Frame(
            cards_frame,
            bg=BG_CARD,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=BORDER_COLOR,
            highlightthickness=2,
            padx=16,
            pady=16,
            cursor="hand2"
        )
        self.card_review.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

        lbl_icono_review = tk.Label(
            self.card_review,
            text="🔍",
            font=("Segoe UI Emoji", 30),
            bg=BG_CARD,
            fg="#34d399"
        )
        lbl_icono_review.pack(pady=(0, 4))

        lbl_titulo_review = tk.Label(
            self.card_review,
            text="Verificar Bug Corregido",
            font=FONT_CARD_TITLE,
            fg="#34d399",
            bg=BG_CARD
        )
        lbl_titulo_review.pack(pady=(0, 6))

        desc_review = (
            "• Validación autónoma sobre dispositivo Android\n"
            "• Conexión ADB + Inspección UI con Gemini IA\n"
            "• Lectura de tickets Todoist (Code Review / QA)\n"
            "• Dictamen: Bug Corregido vs Se Mantiene"
        )
        lbl_desc_review = tk.Label(
            self.card_review,
            text=desc_review,
            font=FONT_CARD_DESC,
            fg=TEXT_MUTED,
            bg=BG_CARD,
            justify="left"
        )
        lbl_desc_review.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.btn_review = tk.Button(
            self.card_review,
            text="🔍  Verificar Bug (2)",
            font=FONT_BTN,
            bg=ACCENT_GREEN,
            fg="#ffffff",
            activebackground=ACCENT_GREEN_HOVER,
            activeforeground="#ffffff",
            bd=0,
            pady=8,
            cursor="hand2",
            command=self.abrir_verificar_bug
        )
        self.btn_review.pack(fill=tk.X)

        # Hover effects en tarjetas
        self._configurar_hover(self.card_subir, [lbl_icono_subir, lbl_titulo_subir, lbl_desc_subir], ACCENT_BLUE)
        self._configurar_hover(self.card_review, [lbl_icono_review, lbl_titulo_review, lbl_desc_review], ACCENT_GREEN)

        # Click directo en la tarjeta
        for w in [self.card_subir, lbl_icono_subir, lbl_titulo_subir, lbl_desc_subir]:
            w.bind("<Button-1>", lambda e: self.abrir_subir_reporte())
        for w in [self.card_review, lbl_icono_review, lbl_titulo_review, lbl_desc_review]:
            w.bind("<Button-1>", lambda e: self.abrir_verificar_bug())

        # Footer con atajos de teclado
        lbl_footer = tk.Label(
            container,
            text="⌨️  Atajos de teclado: Pulsa [1] para Subir Reporte • [2] para Verificar Bug • [ESC] para salir",
            font=FONT_FOOTER,
            fg=TEXT_MUTED,
            bg=BG_MAIN
        )
        lbl_footer.pack(side=tk.BOTTOM, anchor="center")

    def _configurar_hover(self, card_frame, hijos, color_borde):
        def _enter(e):
            card_frame.configure(highlightbackground=color_borde)
        def _leave(e):
            card_frame.configure(highlightbackground=BORDER_COLOR)

        card_frame.bind("<Enter>", _enter)
        card_frame.bind("<Leave>", _leave)
        for h in hijos:
            h.bind("<Enter>", _enter)
            h.bind("<Leave>", _leave)

    def vincular_eventos(self):
        self.root.bind("1", lambda e: self.abrir_subir_reporte())
        self.root.bind("2", lambda e: self.abrir_verificar_bug())
        self.root.bind("<Escape>", lambda e: self.root.destroy())

    def abrir_subir_reporte(self):
        self.root.destroy()
        script_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(script_dir, "gui_subir_reporte.py")
        subprocess.Popen([sys.executable, script_path])

    def abrir_verificar_bug(self):
        self.root.destroy()
        script_dir = os.path.dirname(os.path.abspath(__file__))
        gui_review_path = os.path.join(script_dir, "gui_code_review.py")
        code_review_path = os.path.join(script_dir, "code_review.py")

        if os.path.isfile(gui_review_path):
            subprocess.Popen([sys.executable, gui_review_path])
        elif os.path.isfile(code_review_path):
            subprocess.Popen([sys.executable, code_review_path])
        else:
            messagebox.showinfo(
                "En Diseño",
                "El módulo de verificación de bugs está preparándose en 'gui_code_review.py'."
            )


def main():
    root = tk.Tk()
    app = LauncherQA(root)
    root.mainloop()


if __name__ == "__main__":
    main()
