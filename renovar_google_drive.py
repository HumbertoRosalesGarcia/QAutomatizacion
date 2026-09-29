"""
Script de un solo clic para renovar la autorización de Google Drive.
Abre automáticamente tu navegador para que selecciones tu cuenta y permitas el acceso.
"""
import os
import sys

# Asegurar UTF-8
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

script_dir = os.path.dirname(os.path.abspath(__file__))
token_path = os.path.join(script_dir, 'token.json')
credentials_path = os.path.join(script_dir, 'credentials.json')

if os.path.exists(token_path):
    try:
        os.remove(token_path)
        print("🗑️ Token anterior expirado eliminado.")
    except Exception as e:
        print(f"⚠️ No se pudo eliminar token anterior: {e}")

from google_auth_oauthlib.flow import InstalledAppFlow
SCOPES = ['https://www.googleapis.com/auth/drive']

print("\n" + "=" * 60)
print("🔑 RENOVACIÓN DE AUTORIZACIÓN DE GOOGLE DRIVE")
print("=" * 60)
print("Abriendo tu navegador predeterminado (Chrome)...")
print("Por favor, selecciona tu cuenta de Google y haz clic en 'Permitir'.\n")

flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
creds = flow.run_local_server(port=0)

with open(token_path, 'w', encoding='utf-8') as token_file:
    token_file.write(creds.to_json())

print("\n" + "=" * 60)
print("✅ ¡AUTORIZACIÓN DE GOOGLE DRIVE RENOVADA EXITOSAMENTE!")
print("Ya puedes usar 'Generar y Subir Reporte' con normalidad.")
print("=" * 60)
