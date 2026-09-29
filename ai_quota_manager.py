# -*- coding: utf-8 -*-
"""
ai_quota_manager.py
=============================================================================
Gestor y Monitor de Cuotas / Créditos para Motores de Inteligencia Artificial
(Groq Cloud y Google Gemini) para la Suite de QA Automatizado.
=============================================================================
"""

import os
import sys
import re
import time
import json
import datetime
from typing import Dict, Any, Optional
import requests

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


RUTA_CONFIG_IA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_ia.json")
ENDPOINT_GROQ = "https://api.groq.com/openai/v1/chat/completions"

# Caché local para evitar consumir cuota solo por consultar el estado
_CACHE_CUOTAS = {
    "groq": {"datos": None, "timestamp": 0},
    "gemini": {"datos": None, "timestamp": 0}
}


def cargar_config_ia() -> dict:
    if os.path.isfile(RUTA_CONFIG_IA):
        try:
            with open(RUTA_CONFIG_IA, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def formatear_tiempo_restante(segundos_totales: float) -> str:
    """Convierte segundos a formato amigable: 'X horas con Y min' o 'X min con Y seg'."""
    if segundos_totales <= 0:
        return "inmediatamente"

    horas = int(segundos_totales // 3600)
    minutos = int((segundos_totales % 3600) // 60)
    segundos = int(segundos_totales % 60)

    partes = []
    if horas > 0:
        partes.append(f"{horas} {'hora' if horas == 1 else 'horas'}")
    if minutos > 0:
        partes.append(f"{minutos} min")
    if segundos > 0 and horas == 0:
        partes.append(f"{segundos} seg")

    return " con ".join(partes) if partes else f"{int(segundos_totales)} seg"


def parsear_reset_groq(reset_str: str) -> str:
    """Parsea cabeceras de reset de Groq (ej: '1m26.4s', '2h15m', '4.2s') a español."""
    if not reset_str:
        return "en breve"

    h = re.search(r"(\d+)h", reset_str)
    m = re.search(r"(\d+)m", reset_str)
    s = re.search(r"(\d+(?:\.\d+)?)s", reset_str)

    total_seg = 0.0
    if h:
        total_seg += float(h.group(1)) * 3600
    if m:
        total_seg += float(m.group(1)) * 60
    if s:
        total_seg += float(s.group(1))

    return formatear_tiempo_restante(total_seg)


def calcular_reinicio_diario_utc() -> str:
    """Calcula el tiempo restante para el reinicio diario a las 00:00 UTC."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    medianoche_utc = (now_utc + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    diff = medianoche_utc - now_utc
    return formatear_tiempo_restante(diff.total_seconds())


def calcular_reinicio_semanal_utc() -> str:
    """Calcula el tiempo restante para el reinicio semanal (Domingo 00:00 UTC)."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    dias_hasta_domingo = (6 - now_utc.weekday()) % 7
    if dias_hasta_domingo == 0 and (now_utc.hour > 0 or now_utc.minute > 0):
        dias_hasta_domingo = 7
    proximo_reinicio = (now_utc + datetime.timedelta(days=dias_hasta_domingo)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    diff = proximo_reinicio - now_utc
    total_seg = diff.total_seconds()
    dias = int(total_seg // 86400)
    horas = int((total_seg % 86400) // 3600)
    minutos = int((total_seg % 3600) // 60)
    partes = []
    if dias > 0:
        partes.append(f"{dias} {'día' if dias == 1 else 'días'}")
    if horas > 0:
        partes.append(f"{horas}h")
    if dias == 0 and minutos > 0:
        partes.append(f"{minutos}m")
    return " con ".join(partes) if partes else "en breve"


def calcular_reinicio_gemini_diario() -> str:
    """Calcula el tiempo restante para el reinicio de cuota diaria de Gemini (Medianoche PST / UTC-8)."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_pst = now_utc - datetime.timedelta(hours=8)
    medianoche_pst = (now_pst + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    diff = medianoche_pst - now_pst
    return formatear_tiempo_restante(diff.total_seconds())


def calcular_reinicio_gemini_semanal() -> str:
    """Calcula el tiempo restante para el reinicio semanal de Gemini (Domingo Medianoche PST)."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_pst = now_utc - datetime.timedelta(hours=8)
    dias_hasta_domingo = (6 - now_pst.weekday()) % 7
    if dias_hasta_domingo == 0 and (now_pst.hour > 0 or now_pst.minute > 0):
        dias_hasta_domingo = 7
    proximo_reinicio = (now_pst + datetime.timedelta(days=dias_hasta_domingo)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    diff = proximo_reinicio - now_pst
    total_seg = diff.total_seconds()
    dias = int(total_seg // 86400)
    horas = int((total_seg % 86400) // 3600)
    partes = []
    if dias > 0:
        partes.append(f"{dias} {'día' if dias == 1 else 'días'}")
    if horas > 0:
        partes.append(f"{horas}h")
    return " con ".join(partes) if partes else "en breve"


def consultar_cuota_groq(api_key: Optional[str] = None, forzar_refresco: bool = False) -> Dict[str, Any]:
    """Consulta los límites y cuotas restantes de la API de Groq vía headers HTTP."""
    global _CACHE_CUOTAS
    ahora = time.time()

    if not forzar_refresco and _CACHE_CUOTAS["groq"]["datos"]:
        if ahora - _CACHE_CUOTAS["groq"]["timestamp"] < 45:
            return _CACHE_CUOTAS["groq"]["datos"]

    if not api_key:
        cfg = cargar_config_ia()
        api_key = cfg.get("groq_api_key", "").strip()

    if not api_key:
        return {
            "proveedor": "groq",
            "estado": "sin_clave",
            "mensaje": "⚠️ Clave de API de Groq no configurada",
            "tiempo_reinicio": "N/A",
            "reinicio_diario": "N/A",
            "reinicio_semanal": "N/A",
            "req_restantes": 0,
            "req_limite": 0,
            "tokens_restantes": 0,
            "tokens_limite": 0,
            "resumen_texto": "⚠️ Clave de API de Groq no configurada"
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1
    }

    try:
        resp = requests.post(ENDPOINT_GROQ, headers=headers, json=payload, timeout=8.0)
        h = resp.headers

        req_restantes = h.get("x-ratelimit-remaining-requests", "N/A")
        req_limite = h.get("x-ratelimit-limit-requests", "1000")
        tok_restantes = h.get("x-ratelimit-remaining-tokens", "N/A")
        tok_limite = h.get("x-ratelimit-limit-tokens", "8000")
        reset_req_str = h.get("x-ratelimit-reset-requests", "")
        reset_tok_str = h.get("x-ratelimit-reset-tokens", "")

        reset_tiempo = parsear_reset_groq(reset_req_str or reset_tok_str)
        reinicio_diario = calcular_reinicio_diario_utc()
        reinicio_semanal = calcular_reinicio_semanal_utc()

        # Construir resumen informativo completo
        if reset_tiempo and reset_tiempo not in ["inmediatamente", "en breve"] and "hora" not in reset_tiempo:
            tiempo_detalle = f"Ventana: {reset_tiempo} • Diario: {reinicio_diario} • Semanal: {reinicio_semanal}"
        else:
            tiempo_detalle = f"Diario: en {reinicio_diario} • Semanal: en {reinicio_semanal}"

        datos = {
            "proveedor": "groq",
            "estado": "activo" if resp.status_code == 200 else f"error_{resp.status_code}",
            "mensaje": f"Peticiones: {req_restantes}/{req_limite} • Tokens: {tok_restantes}/{tok_limite}",
            "tiempo_reinicio": reset_tiempo,
            "reinicio_diario": reinicio_diario,
            "reinicio_semanal": reinicio_semanal,
            "req_restantes": req_restantes,
            "req_limite": req_limite,
            "tokens_restantes": tok_restantes,
            "tokens_limite": tok_limite,
            "resumen_texto": f"🟢 Groq: {req_restantes}/{req_limite} req | {tiempo_detalle}"
        }

        _CACHE_CUOTAS["groq"] = {"datos": datos, "timestamp": ahora}
        return datos

    except Exception as e:
        reinicio_diario = calcular_reinicio_diario_utc()
        reinicio_semanal = calcular_reinicio_semanal_utc()
        return {
            "proveedor": "groq",
            "estado": "error_red",
            "mensaje": f"Error conectando a Groq: {e}",
            "tiempo_reinicio": "desconocido",
            "reinicio_diario": reinicio_diario,
            "reinicio_semanal": reinicio_semanal,
            "resumen_texto": f"⚠️ Groq: No se pudo verificar cuota ({e}) • Diario: {reinicio_diario} • Semanal: {reinicio_semanal}"
        }


def consultar_cuota_gemini(api_key: Optional[str] = None) -> Dict[str, Any]:
    """Obtiene información estimada y de ciclo de reinicio para Google Gemini Free Tier."""
    reinicio_diario = calcular_reinicio_gemini_diario()
    reinicio_semanal = calcular_reinicio_gemini_semanal()
    datos = {
        "proveedor": "gemini",
        "estado": "activo",
        "mensaje": "Nivel Gratuito: 15 RPM / 1,500 RPD (Peticiones/día)",
        "tiempo_reinicio": reinicio_diario,
        "reinicio_diario": reinicio_diario,
        "reinicio_semanal": reinicio_semanal,
        "req_restantes": "1,500/día",
        "req_limite": "15 RPM",
        "resumen_texto": f"🔵 Gemini: Nivel Gratuito • Restablece Diario: {reinicio_diario} • Semanal: {reinicio_semanal}"
    }
    return datos


def consultar_estado_cuotas(proveedor: str = "groq", api_key: Optional[str] = None, forzar: bool = False) -> Dict[str, Any]:
    """Función unificada para consultar cuotas del proveedor seleccionado ('groq' o 'gemini')."""
    prov = proveedor.lower()
    if "groq" in prov or "deepseek" in prov:
        return consultar_cuota_groq(api_key=api_key, forzar_refresco=forzar)
    else:
        return consultar_cuota_gemini(api_key=api_key)


if __name__ == "__main__":
    print("--- TEST MONITOR DE CUOTAS ---")
    groq_info = consultar_cuota_groq(forzar_refresco=True)
    print("Groq:", groq_info)
    gemini_info = consultar_cuota_gemini()
    print("Gemini:", gemini_info)
