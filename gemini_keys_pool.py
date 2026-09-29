import os
import json
import time
import datetime
import threading
from google import genai

RUTA_KEYS_STATUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_keys_status.json")

# Lista oficial de las 10 API Keys del usuario (proyectos independientes)
CLAVES_GEMINI_POOL = [
    "AQ.Ab8RN6LeHNqtRIdQCP278seJbKRT1h4NsOhTTFxE-3WCsBiuxg",  # API 1
    "AQ.Ab8RN6J0mCPvooMNoiDKrQpygNqZyYWfxQEqtqN4ZMv8_QvIFQ",  # API 2
    "AQ.Ab8RN6IKPnQB4XXNhTVaiohSvzwGaJ-GnEPgmbNFSQidxxWT3w",  # API 3
    "AQ.Ab8RN6KbjTbCJoQcIYC0jzcaUZRrkUSHenBSlr1HLx5v8A8Q5A",  # API 4
    "AQ.Ab8RN6KF2JnpSt5Yk7kUGIHFhY1FHXuucFL4ECJ2JiOyWubUfw",  # API 5
    "AQ.Ab8RN6IEvqi5MS9Iq4goYvX8FWVwIqTE6bzVSXeAvrTo-yNNGg",  # API 6
    "AQ.Ab8RN6Jv30e-L0TONrPP_tRTa9tSXYDDA5VrqOCSnQpwZdoPog",  # API 7
    "AQ.Ab8RN6KbQM5etSgCw7TtalllbRi9p6nMgg2bC_PNlq-4M--euA",  # API 8
    "AQ.Ab8RN6JNf49Zel-JOBNOmx52n5NXHlvOqbGWNmJvF1ngl-MSrg",  # API 9
    "AQ.Ab8RN6KMEPNad8RcqShJzkjFtQ-MqzakLzfsWG-bKn1B0DxgUQ",  # API 10
]


def _hoy_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def enmascarar_clave(key: str) -> str:
    if len(key) > 16:
        return f"{key[:8]}...{key[-6:]}"
    return key


def _print_seguro(msg: str):
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


class GestorPoolClavesGemini:
    """Gestiona el pool de 10 API Keys de Google Gemini con conmutación automática ante 429."""
    _lock = threading.Lock()

    def __init__(self, ruta_archivo: str = RUTA_KEYS_STATUS):
        self.ruta_archivo = ruta_archivo
        self.datos = self._cargar()

    def _cargar(self) -> dict:
        hoy = _hoy_utc()
        if os.path.isfile(self.ruta_archivo):
            try:
                with open(self.ruta_archivo, "r", encoding="utf-8") as f:
                    data = json.load(f)
                claves_existentes = [c.get("key") for c in data.get("claves", [])]
                if data.get("fecha_utc") == hoy and claves_existentes == CLAVES_GEMINI_POOL:
                    return data
            except Exception:
                pass

        # Inicialización de estado diario
        claves_init = []
        for i, k in enumerate(CLAVES_GEMINI_POOL, 1):
            claves_init.append({
                "id": i,
                "key": k,
                "enmascarada": enmascarar_clave(k),
                "agotada_hoy": False,
                "timestamp_429": 0,
                "consumidas": 0,
                "ultimo_error": ""
            })

        nueva_data = {
            "fecha_utc": hoy,
            "indice_activo": 0,
            "claves": claves_init
        }
        self._guardar_directo(nueva_data)
        return nueva_data

    def _guardar_directo(self, data: dict):
        self.datos = data
        try:
            with open(self.ruta_archivo, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def restablecer_todas_las_claves(self):
        """Restablece todas las claves como disponibles (no agotadas)."""
        with self._lock:
            for c in self.datos.get("claves", []):
                c["agotada_hoy"] = False
                c["timestamp_429"] = 0
                c["ultimo_error"] = ""
            self.datos["indice_activo"] = 0
            self._guardar_directo(self.datos)

    def obtener_clave_activa(self) -> tuple:
        """
        Retorna (api_key, indice_humano, info_dict).
        Busca la primera clave no agotada a partir del índice activo actual.
        """
        with self._lock:
            hoy = _hoy_utc()
            if self.datos.get("fecha_utc") != hoy:
                self.datos = self._cargar()

            claves = self.datos.get("claves", [])
            idx_actual = self.datos.get("indice_activo", 0)

            # Buscar desde idx_actual hasta el final, y luego desde 0
            n = len(claves)
            for offset in range(n):
                candidato_idx = (idx_actual + offset) % n
                c_data = claves[candidato_idx]
                if not c_data.get("agotada_hoy", False):
                    self.datos["indice_activo"] = candidato_idx
                    self._guardar_directo(self.datos)
                    return c_data["key"], candidato_idx + 1, c_data

            # Si todas están marcadas como agotadas, devolver la actual de todos modos
            idx_fallback = idx_actual % max(1, n)
            return claves[idx_fallback]["key"], idx_fallback + 1, claves[idx_fallback]

    def obtener_cliente(self) -> tuple:
        """Retorna (genai.Client, api_key, indice_humano)."""
        key, idx, info = self.obtener_clave_activa()
        client = genai.Client(api_key=key)
        return client, key, idx

    def rotar_siguiente_clave(self, motivo: str = "429 Cuota agotada") -> tuple:
        """
        Marca la clave actual como agotada por hoy y conmuta inmediatamente a la siguiente.
        Retorna (nuevo_client, nueva_key, nuevo_indice_humano).
        """
        with self._lock:
            hoy = _hoy_utc()
            if self.datos.get("fecha_utc") != hoy:
                self.datos = self._cargar()

            claves = self.datos.get("claves", [])
            n = len(claves)
            idx_actual = self.datos.get("indice_activo", 0)

            # Marcar actual como agotada
            if 0 <= idx_actual < n:
                claves[idx_actual]["agotada_hoy"] = True
                claves[idx_actual]["timestamp_429"] = time.time()
                claves[idx_actual]["ultimo_error"] = motivo

            # Buscar siguiente disponible
            nuevo_idx = None
            for offset in range(1, n + 1):
                c_idx = (idx_actual + offset) % n
                if not claves[c_idx].get("agotada_hoy", False):
                    nuevo_idx = c_idx
                    break

            if nuevo_idx is None:
                # Todas las 10 claves agotadas por hoy
                _print_seguro(f"\n⚠️ [POOL AGOTADO] Las {n} API Keys de Gemini han consumido sus créditos diarios de Google.")
                nuevo_idx = (idx_actual + 1) % n

            self.datos["indice_activo"] = nuevo_idx
            self._guardar_directo(self.datos)

            nueva_key = claves[nuevo_idx]["key"]
            nuevo_num = nuevo_idx + 1
            _print_seguro(f"\n🔄 [ROTACIÓN AUTOMÁTICA] Conmutando de API Key #{idx_actual + 1} a API Key #{nuevo_num}/{n} ({enmascarar_clave(nueva_key)})...")
            client = genai.Client(api_key=nueva_key)
            return client, nueva_key, nuevo_num

    def registrar_consumo_activo(self):
        """Registra un reporte exitoso en la clave activa."""
        with self._lock:
            claves = self.datos.get("claves", [])
            idx = self.datos.get("indice_activo", 0)
            if 0 <= idx < len(claves):
                claves[idx]["consumidas"] = claves[idx].get("consumidas", 0) + 1
                claves[idx]["timestamp_429"] = 0
                self._guardar_directo(self.datos)

    def registrar_exito_clave(self, key: str):
        with self._lock:
            for c in self.datos.get("claves", []):
                if c.get("key") == key:
                    c["timestamp_429"] = 0
                    c["agotada_hoy"] = False
                    c["ultimo_error"] = ""
                    break
            self._guardar_directo(self.datos)

    def resumen_pool(self) -> dict:
        """Calcula el resumen de claves disponibles y activas para la interfaz."""
        with self._lock:
            hoy = _hoy_utc()
            if self.datos.get("fecha_utc") != hoy:
                self.datos = self._cargar()

            claves = self.datos.get("claves", [])
            total = len(claves)
            agotadas = sum(1 for c in claves if c.get("agotada_hoy", False))
            disponibles = max(0, total - agotadas)
            idx_act = self.datos.get("indice_activo", 0)

            return {
                "total_claves": total,
                "disponibles": disponibles,
                "agotadas": agotadas,
                "indice_activo": idx_act + 1,
                "clave_activa_enmascarada": enmascarar_clave(claves[idx_act]["key"]) if claves else ""
            }


# Instancia global del pool
gestor_pool = GestorPoolClavesGemini()
