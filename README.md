# QAutomatizacion - On The Fly POS

Sistema de automatización inteligente y verificación autónoma de bugs para terminales Android PAX (On The Fly POS).

## Características principales

- **Verificación Autónoma con IA:** Orquestación multimodal integrada con modelos Gemini y Groq.
- **Análisis de Video y Audio Multimodal:** Ingestión de evidencias de video en Google Drive, transcripción por Whisper y extracción atómica de acciones UI.
- **Localización Adaptable de Elementos:** Resolución cognitiva y semántica de elementos en la pantalla del dispositivo Android PAX sin depender de resoluciones fijas.
- **Integración con Todoist:** Sincronización y lectura de tareas y reportes de QA en Todoist.
- **Supervisión de Contexto y Gestión de Cuotas:** Rotación inteligente de claves API de Gemini y optimización de consumo de tokens con Groq.
- **Interfaz Gráfica de Usuario (GUI):** Herramientas interactivas para lanzamiento, revisión y subida de reportes de pruebas.

## Estructura del Proyecto

- `code_review.py`: Motor principal de verificación de bugs, conexión ADB y ciclo de prueba autónomo.
- `gui_code_review.py`: Interfaz gráfica moderna para la orquesta y supervisión del Code Review.
- `subir_reporte_qa.py` / `gui_subir_reporte.py`: Módulos para creación y subida de reportes formateados a Todoist.
- `gemini_keys_pool.py`: Gestor y pool de rotación de claves API de Google Gemini.
- `proveedor_alternativo.py`: Integración con Groq Cloud para inferencia de alta velocidad y ahorro de créditos.
- `onthefly_knowledge_engine.py`: Base de conocimiento contable y de reglas de negocio para On The Fly POS.

## Requisitos y Configuración

1. Python 3.10+
2. Android Debug Bridge (ADB) configurado y conectado al terminal PAX.
3. Dependencias: `google-genai`, `opencv-python`, `requests`, `google-api-python-client`, `google-auth-oauthlib`, etc.
