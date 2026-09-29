import os
import json
import time
import datetime
import threading
import requests
from gemini_keys_pool import gestor_pool

ARCHIVO_CUOTAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_cuotas.json")

LIMITES_DIARIOS_GOOGLE = {
    "gemini-3.6-flash": 200,
    "gemini-3.8-flash": 200,
    "gemini-3.1-pro-preview": 0,
}


class GestorCuotasGemini:
    """Gestiona, persiste y calcula en tiempo real el consumo y porcentaje de cuota para cada modelo de Gemini."""
    _lock = threading.Lock()

    def __init__(self, ruta_archivo: str = ARCHIVO_CUOTAS):
        self.ruta_archivo = ruta_archivo
        self.datos = self._cargar()

    def _hoy_str(self) -> str:
        # Fecha en UTC (Google reinicia cuotas a las 00:00 UTC)
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    def _cargar(self) -> dict:
        hoy = self._hoy_str()
        if os.path.isfile(self.ruta_archivo):
            try:
                with open(self.ruta_archivo, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("fecha_utc") == hoy and "modelos" in data:
                    return data
            except Exception:
                pass

        # Inicialización para nuevo día
        modelos_init = {}
        for mod, lim in LIMITES_DIARIOS_GOOGLE.items():
            modelos_init[mod] = {
                "limite_diario": lim,
                "consumidas": 0,
                "bloqueado_429": (lim == 0),
                "ultimo_error": "Requiere facturación Pay-as-you-go en Google Cloud" if lim == 0 else ""
            }

        nueva_data = {"fecha_utc": hoy, "modelos": modelos_init}
        self._guardar_directo(nueva_data)
        return nueva_data

    def _guardar_directo(self, data: dict):
        try:
            with open(self.ruta_archivo, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def guardar(self):
        with self._lock:
            self._guardar_directo(self.datos)

    def registrar_consumo(self, modelo_id: str, cantidad: int = 1):
        """Incrementa el contador de peticiones consumidas para un modelo."""
        with self._lock:
            hoy = self._hoy_str()
            if self.datos.get("fecha_utc") != hoy:
                self.datos = self._cargar()

            mod_data = self.datos.setdefault("modelos", {}).setdefault(modelo_id, {
                "limite_diario": LIMITES_DIARIOS_GOOGLE.get(modelo_id, 1500),
                "consumidas": 0,
                "bloqueado_429": False,
                "ultimo_error": ""
            })
            mod_data["consumidas"] = mod_data.get("consumidas", 0) + cantidad
            if mod_data["limite_diario"] > 0 and mod_data["consumidas"] >= mod_data["limite_diario"]:
                mod_data["bloqueado_429"] = True
            self._guardar_directo(self.datos)

    def registrar_429(self, modelo_id: str, mensaje: str = ""):
        """Registra un límite temporal de tasa por minuto (RPM). NO infla el contador de peticiones consumidas."""
        with self._lock:
            hoy = self._hoy_str()
            if self.datos.get("fecha_utc") != hoy:
                self.datos = self._cargar()

            mod_data = self.datos.setdefault("modelos", {}).setdefault(modelo_id, {
                "limite_diario": LIMITES_DIARIOS_GOOGLE.get(modelo_id, 1500),
                "consumidas": 0,
                "bloqueado_429": False,
                "ultimo_error": ""
            })
            # OJO: NUNCA inflar consumidas ni bloquear permanentemente por un límite momentáneo de RPM.
            mod_data["timestamp_429"] = time.time()
            mod_data["ultimo_error"] = mensaje or "429 Límite temporal de consultas por minuto alcanzado"
            self._guardar_directo(self.datos)

    def registrar_503(self, modelo_id: str, mensaje: str = ""):
        """Marca el modelo como saturado temporalmente (503 alta demanda en Google)."""
        with self._lock:
            hoy = self._hoy_str()
            if self.datos.get("fecha_utc") != hoy:
                self.datos = self._cargar()

            mod_data = self.datos.setdefault("modelos", {}).setdefault(modelo_id, {
                "limite_diario": LIMITES_DIARIOS_GOOGLE.get(modelo_id, 1500),
                "consumidas": 0,
                "bloqueado_429": False,
                "ultimo_error": ""
            })
            mod_data["timestamp_503"] = time.time()
            mod_data["ultimo_error"] = mensaje or "503 Servidor temporalmente saturado en Google"
            self._guardar_directo(self.datos)

    def registrar_exito(self, modelo_id: str):
        """Confirma que el modelo respondió bien; si estaba en 429 o 503, limpia el bloqueo."""
        with self._lock:
            mod_data = self.datos.setdefault("modelos", {}).setdefault(modelo_id, {
                "limite_diario": LIMITES_DIARIOS_GOOGLE.get(modelo_id, 1500),
                "consumidas": 0,
                "bloqueado_429": False,
                "ultimo_error": ""
            })
            mod_data["bloqueado_429"] = False
            mod_data["timestamp_503"] = 0
            mod_data["timestamp_429"] = 0
            mod_data["ultimo_error"] = ""
            self._guardar_directo(self.datos)

    def obtener_saturacion_modelo(self, modelo_id: str) -> str:
        """
        Calcula el estado del semáforo para el modelo:
          'rojo': Si no se puede usar (bloqueado por 429, límite 0, o 503 reciente).
          'amarillo': Si el tiempo de recuperación está en curso o latencia alta.
          'verde': Si está disponible sin riesgo de fallo.
        """
        with self._lock:
            self.datos = self._cargar()
            mod_data = self.datos.get("modelos", {}).get(modelo_id, {})
            limite = mod_data.get("limite_diario", LIMITES_DIARIOS_GOOGLE.get(modelo_id, 1500))
            bloqueado = mod_data.get("bloqueado_429", False)
        consumidas = mod_data.get("consumidas", 0)
        t_503 = mod_data.get("timestamp_503", 0)
        t_429 = mod_data.get("timestamp_429", 0)

        # Verificar si todo el pool de 10 claves está agotado
        try:
            pool_info = gestor_pool.resumen_pool()
            if pool_info.get("disponibles", 1) == 0:
                return "rojo"
        except Exception:
            pass

        # Solo bloquear en rojo si el límite diario real fue alcanzado o el modelo tiene límite 0
        if limite == 0 or (limite > 0 and consumidas >= limite):
            return "rojo"

        if t_503 > 0:
            tiempo_transcurrido = time.time() - t_503
            if tiempo_transcurrido < 60:
                return "rojo"      # Sigue saturado (1 min)
            elif tiempo_transcurrido < 180:
                return "amarillo"  # Podría haberse recuperado (~50% de probabilidad)

        if t_429 > 0 and (time.time() - t_429) < 45:
            # Si hay más claves en el pool, no hay que esperar
            try:
                if pool_info.get("disponibles", 0) > 1:
                    return "verde"
            except Exception:
                pass
            return "amarillo"      # Esperando descompresión de 30s por minuto

        return "verde"

    def obtener_estado_modelo(self, modelo_id: str) -> dict:
        """Calcula el estado visual exacto, porcentaje y cuota restante para la interfaz."""
        with self._lock:
            self.datos = self._cargar()
            mod_data = self.datos.get("modelos", {}).get(modelo_id, {})
            limite = mod_data.get("limite_diario", LIMITES_DIARIOS_GOOGLE.get(modelo_id, 200))
            consumidas = mod_data.get("consumidas", 0)
            t_503 = mod_data.get("timestamp_503", 0)
            t_429 = mod_data.get("timestamp_429", 0)

        pool_info = gestor_pool.resumen_pool()
        total_claves = pool_info.get("total_claves", 10)
        claves_disp = pool_info.get("disponibles", 10)
        idx_act = pool_info.get("indice_activo", 1)
        clave_mask = pool_info.get("clave_activa_enmascarada", "")

        if limite == 0:
            return {
                "disponibles": 0,
                "total": 0,
                "consumidas": 0,
                "porcentaje": 0.0,
                "porcentaje_str": "0% (Sin cuota gratis)",
                "color": "#ef4444",
                "badge": "🔴 Sin Cuota Gratuita (Límite: 0 en Google Cloud)",
                "detalle": "Este modelo Pro requiere facturación activa Pay-as-you-go en Google Cloud Console."
            }

        if claves_disp == 0 or (limite > 0 and consumidas >= limite):
            return {
                "disponibles": 0,
                "total": limite,
                "consumidas": limite,
                "porcentaje": 0.0,
                "porcentaje_str": f"0% Disponible (0/{limite} restantes)",
                "color": "#ef4444",
                "badge": f"🔴 Pool Agotado (10/10 claves alcanzaron cuota diaria)",
                "detalle": f"Las {total_claves} API Keys de Gemini han consumido sus créditos diarios de Google. Se renuevan a las 00:00 UTC."
            }

        disponibles = max(0, limite - consumidas)
        porcentaje = max(0.0, min(1.0, disponibles / float(limite)))
        pct_entero = int(round(porcentaje * 100))

        # Si el modelo tiene 503 activo reciente:
        if t_503 > 0 and (time.time() - t_503) < 60:
            color = "#ef4444"
            badge = f"🔴 Servidor Congestionado en Google (503 Saturación)"
            detalle = f"Límite diario: {limite} consultas ({disponibles} disponibles). Google experimenta alta demanda momentánea."
        elif t_503 > 0 and (time.time() - t_503) < 180:
            color = "#f59e0b"
            badge = f"🟡 Recuperándose de Saturación (~50% Probable)"
            detalle = f"Límite diario: {limite} consultas ({disponibles} disponibles). Google tuvo picos de demanda recientemente."
        elif t_429 > 0 and (time.time() - t_429) < 45 and claves_disp <= 1:
            seg_restantes = max(1, int(45 - (time.time() - t_429)))
            color = "#f59e0b"
            badge = f"🟡 Límite temporal por minuto (Esperando {seg_restantes}s...)"
            detalle = f"Límite diario: {limite} consultas ({disponibles} disponibles). Has hecho varias peticiones seguidas; Google reanuda en segundos."
        elif porcentaje < 0.15:
            color = "#ef4444"  # Rojo
            badge = f"🔴 Cuota Crítica: Clave #{idx_act}/{total_claves} ({claves_disp} disp.)"
            detalle = f"Pool de {total_claves} claves con rotación automática. Quedan {disponibles}/{limite} consultas ({pct_entero}%)."
        elif porcentaje < 0.40:
            color = "#f59e0b"  # Amarillo/Naranja
            badge = f"🟡 Clave Activa #{idx_act}/{total_claves} ({claves_disp} disp.) | {pct_entero}%"
            detalle = f"Pool de {total_claves} claves activo ({clave_mask}). Conmutación automática ante 429 activada."
        else:
            color = "#10b981"  # Verde
            badge = f"🟢 Clave Activa #{idx_act}/{total_claves} ({claves_disp} disp.) | {pct_entero}%"
            detalle = f"Pool de {total_claves} claves activo ({clave_mask}). Conmutación automática ante 429 activada."

        if modelo_id == "gemini-3.8-flash":
            detalle += " Calidad máxima con análisis técnico profundo."

        return {
            "disponibles": disponibles,
            "total": limite,
            "consumidas": consumidas,
            "porcentaje": porcentaje,
            "porcentaje_str": f"{pct_entero}% Disponible ({disponibles}/{limite})",
            "color": color,
            "badge": badge,
            "detalle": detalle
        }

    def probar_en_vivo_modelo(self, modelo_id: str, api_key: str) -> dict:
        """
        Verifica si Google tiene cuota disponible para el modelo consultando
        los metadatos del modelo (GET), SIN hacer generate_content.
        Esto NO consume ningún crédito.
        """
        if not api_key:
            return self.obtener_estado_modelo(modelo_id)

        # GET a la metadata del modelo: respuesta 200 = API activa, 429 = sin cuota
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo_id}?key={api_key}"
        try:
            r = requests.get(url, timeout=8.0)
            if r.status_code == 200:
                # OJO: NO llamar a registrar_exito() aquí porque un GET a metadatos
                # siempre responde 200 en el Gateway y no refleja si el motor de inferencia está con 503.
                pass
            elif r.status_code == 429:
                self.registrar_429(modelo_id, r.text[:200])
            elif r.status_code == 503:
                self.registrar_503(modelo_id, r.text[:200])
        except Exception:
            pass
        return self.obtener_estado_modelo(modelo_id)


# Instancia global para ser usada en todo el proyecto
gestor_cuotas = GestorCuotasGemini()
