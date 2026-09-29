# -*- coding: utf-8 -*-
import os, re, json, subprocess
from typing import Dict, List, Any, Optional

class OnTheFlyKnowledgeBase:
    APK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'review_temp', 'ontheflypos.apk')
    AAPT_PATH = r'C:\Users\Administrador\AppData\Local\Android\Sdk\build-tools\35.0.0\aapt.exe'
    CACHE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'review_temp', 'onthefly_knowledge_base.json')

    MODULOS = {
        'DASHBOARD': {
            'layout': 'res/layout-land/fragment_dash.xml',
            'botones_principales': [
                {'id': 'btnQuickSale', 'nombre': 'QUICK SALE', 'descripcion': 'Venta directa sin mesa'},
                {'id': 'btnTables', 'nombre': 'TABLES', 'descripcion': 'Plano de mesas y comensales'},
                {'id': 'btnTabs', 'nombre': 'TABS', 'descripcion': 'Cuentas abiertas en barra'},
                {'id': 'btnReports', 'nombre': 'REPORTS', 'descripcion': 'Cierre de caja y reportes X/Z'},
                {'id': 'btnSettings', 'nombre': 'SETTINGS', 'descripcion': 'Configuracion y perifericos'},
                {'id': 'btnSync', 'nombre': 'SYNC', 'descripcion': 'Sincronizacion remota'}
            ]
        },
        'CATALOGO_POS': {
            'layout': 'res/layout-land/activity_main2.xml',
            'elementos_clave': {
                'categorias': 'category_item',
                'productos': 'product_item',
                'carrito': 'activity_shopping_cart',
                'boton_pay': 'payment',
                'boton_loyalty': 'buttonorderinfo',
                'notas_orden': 'button_note',
                'descartar_venta': 'btn_discard_sale'
            }
        },
        'BUSQUEDA_CLIENTES': {
            'layout': 'res/layout/dialog_fragment_clients.xml',
            'elementos_clave': {
                'campo_busqueda': 'txtSearchClient',
                'lista_resultados': 'ordersList',
                'boton_escanear_qr': 'btnScanQr',
                'boton_cerrar': 'btnClose'
            },
            'contrato_operativo': (
                'Para vincular CUALQUIER cliente (ej. Humberto Gold, Tatiana o cualquier comensal): '
                '1. Abrir dialogo de clientes tocando Phone Number / Name o Ticket Info. '
                '2. Escribir el nombre en txtSearchClient. '
                '3. Seleccionar la fila del cliente en ordersList (coordenadas Y > 200). '
                '4. Verificar que aparezca el badge de membresia (txtMembershipBadge) en el carrito o checkout.'
            )
        },
        'PRODUCTO_PESABLE': {
            'layout': 'res/layout/dialog_quantity.xml',
            'elementos_clave': {
                'peso_neto': 'net weight',
                'boton_peso_manual': 'manual weight',
                'campo_entrada_peso': 'et_weight',
                'boton_agregar_carrito': 'add to cart'
            },
            'contrato_operativo': (
                'Para productos pesables (ej. Queso Regular, items por libra o kilo): '
                '1. Tocar el producto en el catalogo para abrir dialog_quantity.xml. '
                '2. Tocar el boton Manual Weight. '
                '3. Digitar el peso en el teclado numerico (ej. 200 para 2.00 lb) y presionar OK. '
                '4. Tocar Add to Cart para agregar el producto con su peso calculado.'
            )
        },
        'PANTALLA_PAGOS': {
            'layout': 'res/layout-land/activity_payment_new.xml',
            'subcontenedores': {
                'resumen': 'fragment_resumen_order_v3.xml',
                'metodos_pago': 'fragment_paymen_option.xml'
            },
            'elementos_clave': {
                'subtotal_items': 'txtSubTotalItem',
                'descuento_membresia': 'txtMembershipDiscount',
                'descuento_orden': 'txtOrderDiscount',
                'lista_descuentos': 'recyclerView_Discounts',
                'impuesto_orden': 'tvAmountTax',
                'cargo_servicio': 'txtServiceCharge',
                'total_venta': 'txtTotalSale',
                'monto_cash': 'txtCashValue',
                'monto_debit': 'txtDebitCardValue',
                'monto_credit_card': 'txtCreditCardValue',
                'boton_add_discount': 'btnAddDiscount',
                'boton_split': 'btnSplitAmount',
                'boton_void': 'btnVoid',
                'nombre_cliente': 'txtCustomer'
            },
            'contrato_operativo': (
                'En la pantalla de pagos: '
                '1. La columna izquierda muestra el desglose: Subtotal, Descuentos, Taxes y Service Charge. '
                '2. La columna derecha muestra metodos rapidos (Cash, Debit, Credit Card) y operaciones. '
                '3. Para aplicar descuentos manuales a la orden, pulsar btnAddDiscount (deslizar hacia arriba si no esta visible).'
            )
        },
        'MODAL_DESCUENTOS': {
            'layout': 'res/layout/fragment_discount_dialog.xml',
            'elementos_clave': {
                'titulo': 'lbltitle',
                'check_descuento_item': 'btnDiscountItem',
                'grilla_razones': 'Botonera',
                'razones_items': 'name_discont',
                'porcentajes_rapidos': 'btnDefaultDiscount',
                'campo_notas': 'txtDeleteAddNotes',
                'boton_aplicar': 'btnDiscountCompSave'
            },
            'contrato_operativo': (
                'Para aplicar descuento manual del 50% (Spill Comp): '
                '1. Seleccionar la razon en la grilla (ej. Comp N/A). '
                '2. Seleccionar el porcentaje 50%. '
                '3. Presionar btnDiscountCompSave (Apply Discount). '
                '4. Si la app exige autorizacion administrativa, ingresar PIN 1111 en dialog_pin.xml. '
                '5. Si el sistema muestra el toast de prioridad (This order has a membership discount with priority...), '
                'significa que el POS bloquea la combinacion de multiples descuentos para evitar inconsistencias de redondeo.'
            )
        },
        'AUTORIZACION_PIN': {
            'layout': 'res/layout/dialog_pin.xml',
            'elementos_clave': {
                'indicador_pin': 'circlePin',
                'teclado': ['btn1', 'btn2', 'btn3', 'btn4', 'btn5', 'btn6', 'btn7', 'btn8', 'btn9', 'btn0'],
                'borrar': 'btnRemove'
            },
            'pin_default_qa': '1111'
        }
    }

    _instancia = None

    @classmethod
    def obtener_instancia(cls):
        if cls._instancia is None:
            cls._instancia = cls()
        return cls._instancia

    def __init__(self):
        self.recursos = {}
        self.hex_a_nombre = {}
        self._cargar_o_extraer_recursos()

    def _cargar_o_extraer_recursos(self):
        if os.path.exists(self.CACHE_DB_PATH):
            try:
                with open(self.CACHE_DB_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.hex_a_nombre = data.get('hex_map', {})
                    self.recursos = data.get('resources', {})
                if len(self.hex_a_nombre) >= 15000:
                    return
            except Exception:
                pass

        if not os.path.exists(self.AAPT_PATH) or not os.path.exists(self.APK_PATH):
            return

        try:
            cmd = [self.AAPT_PATH, 'dump', 'resources', self.APK_PATH]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
            hex_map = {}
            resources_by_type = {}
            for line in res.stdout.splitlines():
                m = re.search(r'spec resource (0x[0-9a-fA-F]+) (com\.kubilabs\.ontheflypos:([^/]+)/([^:]+)):', line)
                if m:
                    h = m.group(1).lower()
                    full_name = m.group(2)
                    res_type = m.group(3)
                    res_id = m.group(4)
                    hex_map[h] = full_name
                    if res_type not in resources_by_type:
                        resources_by_type[res_type] = []
                    resources_by_type[res_type].append(res_id)

            self.hex_a_nombre = hex_map
            self.recursos = resources_by_type

            os.makedirs(os.path.dirname(self.CACHE_DB_PATH), exist_ok=True)
            with open(self.CACHE_DB_PATH, 'w', encoding='utf-8') as f:
                json.dump({'hex_map': hex_map, 'resources': resources_by_type}, f, ensure_ascii=False)
        except Exception:
            pass

    def resolver_recurso(self, hex_id: str) -> str:
        h = hex_id.lower().replace('@', '')
        return self.hex_a_nombre.get(h, hex_id)

    def obtener_contrato_procedimiento(self, accion_solicitada: str) -> str:
        a_lower = accion_solicitada.lower()
        if any(k in a_lower for k in ['cliente', 'membresia', 'customer', 'gold', 'tatiana', 'vincular']):
            return self.MODULOS['BUSQUEDA_CLIENTES']['contrato_operativo']
        elif any(k in a_lower for k in ['pesable', 'peso', 'balanza', 'lb', 'queso']):
            return self.MODULOS['PRODUCTO_PESABLE']['contrato_operativo']
        elif any(k in a_lower for k in ['descuento', 'spill comp', 'comp', '50%']):
            return self.MODULOS['MODAL_DESCUENTOS']['contrato_operativo']
        elif any(k in a_lower for k in ['pago', 'pay', 'checkout', 'credit card', 'redondeo']):
            return self.MODULOS['PANTALLA_PAGOS']['contrato_operativo']
        return 'Procedimiento estandar On The Fly: interactuar con las vistas identificadas por resource-id o texto visible.'

    def auditar_redondeo_matematico(self, desglose: Dict[str, float], total_observado_cc: float) -> Dict[str, Any]:
        subtotal = desglose.get('subtotal', 0.0)
        desc_membresia = desglose.get('descuento_membresia', 0.0)
        desc_orden = desglose.get('descuento_orden', 0.0)
        taxes = desglose.get('impuestos', 0.0)
        service_charge = desglose.get('service_charge', 0.0)

        base_imponible = round(subtotal - desc_membresia - desc_orden, 2)
        total_calculado = round(base_imponible + taxes + service_charge, 2)
        discrepancia = round(total_observado_cc - total_calculado, 2)

        return {
            'subtotal': subtotal,
            'descuento_membresia': desc_membresia,
            'descuento_orden': desc_orden,
            'impuestos': taxes,
            'service_charge': service_charge,
            'total_calculado': total_calculado,
            'total_observado_cc': total_observado_cc,
            'discrepancia_centavos': int(round(discrepancia * 100)),
            'hay_discrepancia': abs(discrepancia) > 0.001
        }
