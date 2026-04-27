# dian_invoice_extractor.py

# -*- coding: utf-8 -*-
import base64
import re
import json
import logging
from datetime import datetime, timedelta
import time
from difflib import SequenceMatcher

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

CODIGO_RE = re.compile(r"^(?P<code>[A-Z]{2}\d{2})(?:\s+(?P<cc>\d+))?\s+(?P<body>.+)$")


class DianInvoiceExtractor(models.Model):
    _inherit = "dian.invoice.extractor"

    # -------------------------
    # Campos nuevos (coherentes con tu módulo y tus vistas)
    # -------------------------
    compania_id = fields.Many2one("res.company", string="Compañía", tracking=True, default=lambda self: self.env.company)

    # Compatibilidad: si algún código aún usa company_id, esto evita errores.
    company_id = fields.Many2one("res.company", related="compania_id", store=True, readonly=False)

    proveedor_id = fields.Many2one("res.partner", string="Proveedor", tracking=True)
    servicio_id = fields.Many2one("maestro.servicios", string="Servicio", tracking=True)

    fecha_efectiva = fields.Date(string="Fecha efectiva", tracking=True)
    monto_documento = fields.Monetary(string="Valor a pagar", currency_field="currency_id", tracking=True)
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id.id)

    es_xml = fields.Boolean(string="Es XML", readonly=True, tracking=True)

    estado_ocr = fields.Selection(
        [("no_aplica", "No aplica (XML)"),
         ("pendiente", "Pendiente validación"),
         ("validado", "Validado")],
        string="Estado OCR",
        default="no_aplica",
        tracking=True,
    )
    texto_ocr = fields.Text(string="Texto OCR", readonly=True)
    datos_ocr_json = fields.Text(string="Datos OCR (JSON)", readonly=True)

    naturaleza_documento = fields.Selection(
        [("productos", "Productos"),
         ("servicios", "Servicios"),
         ("mixto", "Mixto"),
         ("desconocido", "Desconocido")],
        string="Naturaleza",
        default="desconocido",
        tracking=True,
    )
    ciudad_documento = fields.Char(string="Ciudad (documento)", tracking=True)

    bloqueado = fields.Boolean(string="Bloqueado", default=False, tracking=True)
    motivo_bloqueo = fields.Text(string="Motivo bloqueo", tracking=True)

    autorizacion_servicio_id = fields.Many2one(
        "autorizacion.servicio", string="Autorización vigente", tracking=True
    )

    factura_proveedor_id = fields.Many2one(
        "account.move", string="Factura proveedor", readonly=True, tracking=True, ondelete="set null"
    )

    ciudad_prestacion = fields.Char(string="Ciudad Prestación", help="Ciudad extraída por OCR para asignación analítica")



    def _crear_linea_generica(self):
        """Crea una línea genérica si no hay líneas pero hay monto total."""
        self.invoice_lines.unlink()
        self.env["dian.invoice.line"].create({
            'invoice_id': self.id,
            'sequence': 1,
            'description': _('Servicio según documento'),
            'quantity': 1.0,
            'price_unit': self.monto_documento,
            'line_extension_amount': self.monto_documento,
            'tax_amount': 0.0,
            'tax_percent': 0.0,
        })





    def _crear_lineas_desde_line_items(self, line_items):
        """Crea líneas de factura a partir de line_items, manejando valores nulos."""
        self.invoice_lines.unlink()  # Eliminar líneas anteriores
        Line = self.env["dian.invoice.line"]
        seq = 1

        # Cargar etiquetas en memoria para Fuzzy Matching
        etiquetas = self.env['maestro.servicios.etiqueta'].search([])

        # Asegurar que line_items sea una lista
        if not isinstance(line_items, list):
            line_items = [line_items] if line_items else []

        # Lógica Anti-Ceros: Si todas las líneas tienen valor 0, pero hay un monto global, inyectarlo a la primera
        if line_items and self.monto_documento:
            todos_cero = all(self._parse_money(it.get('valor_total_linea', 0.0)) == 0 for it in line_items if isinstance(it, dict))
            if todos_cero:
                _logger.warning("Lógica Anti-Ceros: Todas las líneas extraídas tienen valor 0. Inyectando monto global %s a la primera línea.", self.monto_documento)
                for it in line_items:
                    if isinstance(it, dict):
                        it['valor_total_linea'] = self.monto_documento
                        break  # Solo a la primera línea

        for item in line_items:
            if not item or not isinstance(item, dict):
                continue

            # Forzar cantidad a 1 y usar el valor total como precio unitario sin cálculos
            cantidad = 1.0
            valor_total = self._parse_money(item.get('valor_total_linea', 0.0))
            price_unit = valor_total
            base_sin_iva = valor_total

            descripcion = (item.get('descripcion') or '').strip()
            
            # Fuzzy Matching para asignar servicio
            servicio_asignado_id = False
            umbral_minimo = 0.90 if getattr(self, 'es_xml', False) else 0.40
            
            if descripcion:
                mejor_ratio = 0.0
                mejor_etiqueta = None
                for etiqueta in etiquetas:
                    if not etiqueta.name:
                        continue
                    ratio = SequenceMatcher(None, descripcion.lower(), etiqueta.name.lower()).ratio()
                    if ratio > mejor_ratio:
                        mejor_ratio = ratio
                        mejor_etiqueta = etiqueta

                if mejor_ratio >= umbral_minimo and mejor_etiqueta:
                    servicio_asignado_id = mejor_etiqueta.servicio_id.id
                    _logger.info("OCR Fuzzy Match: Línea '%s' asignada al servicio '%s' (Ratio: %.2f%%)", descripcion, mejor_etiqueta.servicio_id.name, mejor_ratio * 100)
                else:
                    _logger.info("OCR Fuzzy Match: Línea '%s' sin coincidencia suficiente (Mejor ratio: %.2f%%, requerido: %.2f%%)", descripcion, mejor_ratio * 100, umbral_minimo * 100)

            # Fallback (El Paracaídas del Servicio)
            if not servicio_asignado_id:
                servicio_asignado_id = self.servicio_id.id if self.servicio_id else False
                _logger.info("OCR Paracaídas: Asignando servicio por defecto de la cabecera (ID: %s)", servicio_asignado_id)

            vals = {
                'invoice_id': self.id,
                'sequence': seq,
                'product_code': (item.get('codigo') or '').strip(),
                'description': descripcion,
                'quantity': cantidad,
                'price_unit': round(price_unit, 6),
                'line_extension_amount': round(base_sin_iva, 2),
                'tax_amount': 0.0,
                'tax_percent': 0.0,
                'tax_scheme': '',
                'servicio_id': servicio_asignado_id,
            }
            Line.create(vals)
            seq += 1



    def _mapear_datos_llm_a_campos(self, extracted):
        """Convierte el dict del LLM en un dict con valores normalizados para los campos."""
        datos = {}

        # --- FECHA DE EMISIÓN ---
        fecha_emision = extracted.get('fecha_emision')
        if fecha_emision:
            try:
                # Intentar varios formatos comunes
                fecha_str = str(fecha_emision).strip()
                for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
                    try:
                        datos['fecha_efectiva'] = datetime.strptime(fecha_str, fmt).date()
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        # Si no hay fecha_emision, probar con periodo_fin
        if not datos.get('fecha_efectiva') and extracted.get('periodo_fin'):
            try:
                fecha_str = str(extracted['periodo_fin']).strip()
                for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
                    try:
                        datos['fecha_efectiva'] = datetime.strptime(fecha_str, fmt).date()
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        # --- NÚMERO DE FACTURA ---
        if extracted.get('numero_factura'):
            datos['invoice_number'] = str(extracted['numero_factura']).strip()

        # --- NIT PROVEEDOR (solo dígitos) ---
        nit_prov = extracted.get('nit_proveedor')
        if nit_prov:
            nit_prov = re.sub(r'\D', '', str(nit_prov))
            if len(nit_prov) > 9:
                nit_prov = nit_prov[:9]  # Colombia: NIT de 9 dígitos
            datos['nit_proveedor'] = nit_prov

        # --- NIT CLIENTE ---
        nit_cli = extracted.get('id_cliente')
        if nit_cli:
            nit_cli = re.sub(r'\D', '', str(nit_cli))
            if len(nit_cli) > 9:
                nit_cli = nit_cli[:9]
            datos['nit_cliente'] = nit_cli

        # --- NOMBRE PROVEEDOR ---
        if extracted.get('nombre_proveedor'):
            datos['nombre_proveedor'] = str(extracted['nombre_proveedor']).strip()

        # --- TOTAL (convertir a float) ---
        total = extracted.get('total')
        if total is not None:
            datos['monto_documento'] = self._parse_money(total)

        # --- TIPO DE DOCUMENTO (opcional, para clasificación) ---
        if extracted.get('tipo_documento'):
            tipo = str(extracted['tipo_documento']).lower()
            if 'invoice' in tipo or 'factura' in tipo:
                datos['tipo_documento'] = 'invoice'
            elif 'charge' in tipo or 'cuenta' in tipo:
                datos['tipo_documento'] = 'charge'
            else:
                datos['tipo_documento'] = 'other'

        return datos




    # =====================================================================
    # FIX RAÍZ: process_xml_invoice() NO debe intentar parsear PDF/IMG
    # (porque el modelo original lo llama automáticamente en create/write)
    # =====================================================================
    def process_xml_invoice(self):
        """
        El modelo original (transcriptor_ocr) llama process_xml_invoice() en create/write
        SIN validar el tipo de archivo. Este override evita que intente parsear PDF/IMG.
        """
        for rec in self:
            data, filename = rec._obtener_binario_y_nombre()
            if not data:
                return True
            if not rec._parece_xml(data, filename):
                # No es XML => NO ejecutar el parser XML del módulo original.
                _logger.info("process_xml_invoice(): archivo NO XML (%s). Se omite parseo XML.", filename)
                return True

            # Sí es XML => ejecutar el método original
            return super(DianInvoiceExtractor, rec).process_xml_invoice()

        return True

    # -------------------------
    # Hooks create/write
    # -------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Heredar servicio_id si viene con autorizacion_servicio_id y no trae servicio
            if not vals.get('servicio_id') and vals.get('autorizacion_servicio_id'):
                auth = self.env['autorizacion.servicio'].browse(vals['autorizacion_servicio_id'])
                if auth.exists() and auth.servicio_id:
                    vals['servicio_id'] = auth.servicio_id.id

        records = super().create(vals_list)
        # Autoprocesar si entró archivo al crear
        for rec, vals in zip(records, vals_list):
            if rec._cambio_archivo_en_vals(vals):
                rec.action_procesar_documento()
        return records

    def write(self, vals):
        res = super().write(vals)

        # Reprocesar si cambió el archivo
        if self._cambio_archivo_en_vals(vals):
            self.with_context(evitar_bucle_proceso=True).action_procesar_documento()

        # Re-evaluar bloqueo si cambian campos clave
        if {"compania_id", "proveedor_id", "servicio_id", "fecha_efectiva", "monto_documento", "estado_ocr"} & set(vals.keys()):
            self._evaluar_bloqueo()

        if 'servicio_id' in vals and vals['servicio_id']:
            for rec in self:
                lineas_vacias = rec.invoice_lines.filtered(lambda l: not l.servicio_id)
                if lineas_vacias:
                    lineas_vacias.write({'servicio_id': vals['servicio_id']})

        return res

    def _cambio_archivo_en_vals(self, vals):
        posibles = {"file_data", "archivo", "original_file", "attachment_id", "file", "datas", "file_name"}
        return bool(posibles & set(vals.keys()))

    # -------------------------
    # Acciones UI
    # -------------------------
    def action_procesar_documento(self):
        action_res = None
        for rec in self:
            data, filename = rec._obtener_binario_y_nombre()
            if not data:
                continue

            if rec._parece_xml(data, filename):
                rec._procesar_xml_usando_extractor(data, filename)
            else:
                res = rec._procesar_ocr(data, filename)
                if isinstance(res, dict) and res.get('type') == 'ir.actions.client':
                    action_res = res
            rec._aplicar_reglas_asignacion_servicio()
            rec._evaluar_bloqueo()
        return action_res

    def action_validar_ocr(self):
        for rec in self:
            if rec.estado_ocr != "pendiente":
                continue
            rec.estado_ocr = "validado"

            # Si es OCR y no hay líneas, generarlas
            if not rec.es_xml and not rec.invoice_lines:
                rec._generar_invoice_lines_desde_ocr()

            rec._aplicar_reglas_asignacion_servicio()
            rec._evaluar_bloqueo()

    def action_solicitar_autorizacion(self):
        self.ensure_one()
        if not self.bloqueado:
            raise UserError(_("Este documento no está bloqueado."))

        categoria_id = self.env["ir.config_parameter"].sudo().get_param(
            "causacion_terceros_autorizaciones.categoria_aprobacion_id"
        )
        if not categoria_id:
            raise UserError(_(
                "No está configurada la categoría de aprobación.\n"
                "Ve a Ajustes > Causación terceros > Categoría de aprobación."
            ))

        vals = {
            "name": _("Autorización: %s") % (self.servicio_id.display_name if self.servicio_id else _("(sin servicio)")),
            "category_id": int(categoria_id),
            "request_owner_id": self.env.user.id,
            "compania_id": self.compania_id.id,
            "proveedor_id": self.proveedor_id.id,
            "servicio_id": self.servicio_id.id,
            "fecha_inicio": self.fecha_efectiva or fields.Date.context_today(self),
            "monto_mensual_fijo": self.monto_documento,
            "tipo_contratacion": "unica",
        }
        req = self.env["approval.request"].create(vals)

        return {
            "type": "ir.actions.act_window",
            "name": _("Solicitud de aprobación"),
            "res_model": "approval.request",
            "res_id": req.id,
            "view_mode": "form",
            "target": "current",
        }




    def _construir_lineas_factura_desde_invoice_lines(self): 
        self.ensure_one() 

        if not self.invoice_lines: 
            raise UserError(_("No hay líneas DIAN para construir la factura.")) 

        # exigir servicio por línea (para cuentas distintas) 
        faltantes = self.invoice_lines.filtered(lambda l: not getattr(l, "servicio_id", False)) 
        if faltantes: 
            cods = ", ".join([(l.product_code or "SIN-COD") for l in faltantes[:10]]) 
            raise UserError(_("Hay líneas sin servicio asignado. Ejemplos: %s") % cods) 

        cmds = [] 
        
        # Obtener el porcentaje dinámicamente desde la compañía 
        porcentaje = self.compania_id.porcentaje_iva_mayor_valor if self.compania_id else self.env.company.porcentaje_iva_mayor_valor 
        ratio_mv = porcentaje / 100.0 
        ratio_desc = 1.0 - ratio_mv 
        
        # Acumulador global para el IVA Mayor Valor 
        total_iva_mayor_valor_acumulado = 0.0 

        for l in self.invoice_lines: 
            servicio = l.servicio_id 

            # tu maestro.servicios usa linea_exclusion_ids, y la cuenta es 'cuentas', impuesto 'grupo_impuestos' 
            if not servicio.linea_exclusion_ids: 
                raise UserError(_("El servicio '%s' no tiene líneas configuradas (linea_exclusion_ids).") % servicio.display_name) 

            cfg = servicio.linea_exclusion_ids[0] 
            if not cfg.cuentas: 
                raise UserError(_("El servicio '%s' no tiene cuenta (cuentas).") % servicio.display_name) 

            qty = l.quantity or 1.0 
            base = l.line_extension_amount or 0.0 
            # Mantenemos alta precisión para el cálculo base 
            price_unit = round((base / qty) if qty else base, 6) 

            vals = { 
                "name": (l.description or servicio.display_name), 
                "quantity": qty, 
                "price_unit": price_unit, 
                "account_id": cfg.cuentas.id, 
            } 
            
            # Recolectar impuestos EXCLUSIVAMENTE desde el maestro.servicios 
            tax_ids = [] 
            
            if servicio and servicio.linea_exclusion_ids: 
                for linea in servicio.linea_exclusion_ids: 
                    if linea.grupo_impuestos: 
                        for impuesto in linea.grupo_impuestos: 
                            if impuesto.id not in tax_ids: 
                                tax_ids.append(impuesto.id) 
                                
            # Identificar si hay IVA (impuesto con monto > 0) 
            tiene_iva = False 
            other_tax_ids = [] 
            tasa_iva = 0.0 
            
            if tax_ids: 
                impuestos = self.env['account.tax'].browse(tax_ids) 
                for imp in impuestos: 
                    if imp.amount > 0: # Asumimos que IVA tiene amount > 0 
                        tiene_iva = True 
                        tasa_iva = imp.amount / 100.0 # Convertir 19.0 a 0.19 
                    else: 
                        other_tax_ids.append(imp.id) 
                        
            # Prorrateo Nativo 
            if ratio_mv > 0.0 and tiene_iva: 
                # Obtener cuenta Mayor Valor (última fila de exclusión del servicio) 
                cuenta_mayor_valor_id = False 
                if servicio.linea_exclusion_ids: 
                    cuenta_mayor_valor_id = servicio.linea_exclusion_ids[-1].cuentas.id 
                    
                if not cuenta_mayor_valor_id: 
                    raise UserError(_("El servicio '%s' no tiene cuenta configurada en su última línea de exclusión para el IVA Mayor Valor Gasto.") % servicio.display_name) 
                
                # Cálculo virtual del IVA Mayor Valor a acumular 
                monto_iva_mv = (price_unit * ratio_mv) * tasa_iva 
                total_iva_mayor_valor_acumulado += (monto_iva_mv * qty) 
                
                # Línea 1 (Gasto 90% - Descontable) 
                vals_desc = vals.copy() 
                vals_desc["price_unit"] = round(price_unit * ratio_desc, 6) 
                if tax_ids: 
                    vals_desc["tax_ids"] = [(6, 0, tax_ids)] 
                cmds.append((0, 0, vals_desc)) 
                
                # Línea 2 (Gasto 10% - Mayor Valor) 
                vals_mv = vals.copy() 
                pct_entero = int(porcentaje) if porcentaje.is_integer() else round(porcentaje, 2) 
                vals_mv["name"] = vals_mv["name"] + f" - {pct_entero}% Base Gasto" 
                vals_mv["price_unit"] = round(price_unit * ratio_mv, 6) 
                vals_mv["account_id"] = cfg.cuentas.id # LA MISMA cuenta de gasto 
                if other_tax_ids: 
                    vals_mv["tax_ids"] = [(6, 0, other_tax_ids)] # Retenciones sí, IVA no 
                else: 
                    vals_mv.pop("tax_ids", None) # Remove if empty 
                cmds.append((0, 0, vals_mv)) 
                
                subtotal_calculado = round(qty * round(price_unit * ratio_desc, 6), 2) + round(qty * round(price_unit * ratio_mv, 6), 2) 
                _logger.info("Línea dividida por Prorrateo IVA: Ratio MV %s, Ratio Desc %s, IVA Acumulado: %s", ratio_mv, ratio_desc, monto_iva_mv) 
            else: 
                if tax_ids: 
                    vals["tax_ids"] = [(6, 0, tax_ids)] 
                cmds.append((0, 0, vals)) 
                subtotal_calculado = round(qty * price_unit, 2) 

            # Validación de seguridad: Ajuste de redondeo por pérdida de precisión 
            base_esperada = round(base, 2) 
            diferencia = round(base_esperada - subtotal_calculado, 2) 
            
            if diferencia != 0.0: 
                ajuste_vals = { 
                    "name": _("Ajuste automático de redondeo OCR"), 
                    "quantity": 1.0, 
                    "price_unit": diferencia, 
                    "account_id": cfg.cuentas.id, 
                } 
                if tax_ids: 
                    ajuste_vals["tax_ids"] = [(6, 0, tax_ids)] 
                    
                cmds.append((0, 0, ajuste_vals)) 
                _logger.warning("OCR Ajuste de redondeo: Diferencia de %s detectada en la línea '%s'. Línea de ajuste inyectada.", diferencia, l.description) 

        # Inyectar línea final de IVA Mayor Valor Acumulado si aplica 
        if total_iva_mayor_valor_acumulado > 0: 
            cuenta_mayor_valor_final_id = False 
            servicio_final = self.servicio_id or (self.invoice_lines[-1].servicio_id if self.invoice_lines else False) 
            if servicio_final and servicio_final.linea_exclusion_ids: 
                cuenta_mayor_valor_final_id = servicio_final.linea_exclusion_ids[-1].cuentas.id 
                
            if cuenta_mayor_valor_final_id: 
                vals_iva_acumulado = { 
                    "name": "Total IVA Mayor Valor Gasto", 
                    "quantity": 1.0, 
                    "price_unit": round(total_iva_mayor_valor_acumulado, 2), 
                    "account_id": cuenta_mayor_valor_final_id, 
                } 
                cmds.append((0, 0, vals_iva_acumulado)) 
                _logger.info("Inyectada línea global de Total IVA Mayor Valor Gasto por monto: %s", vals_iva_acumulado["price_unit"]) 
            else: 
                _logger.error("No se pudo inyectar la línea global de IVA Mayor Valor porque no se encontró cuenta en el servicio.") 

        return cmds




    def action_crear_factura_proveedor(self):
        """
        Crea un account.move (in_invoice) validando autorizaciones y 
        mapeando las líneas del OCR a líneas contables.
        """
        for rec in self:
            rec._asegurar_listo_para_contabilizar()

            if rec.factura_proveedor_id:
                return rec._action_abrir_factura(rec.factura_proveedor_id)

            # Validación de Autorización
            if not rec.autorizacion_servicio_id:
                raise UserError(_(
                    "No se puede crear la factura: Este documento no tiene una autorización "
                    "de servicio vigente asignada (autorizacion_servicio_id)."
                ))

            # Obtener diario
            company = rec.compania_id or self.env.company
            if getattr(rec, 'es_xml', False):
                journal_id = company.diario_defecto_xml_id.id
            else:
                journal_id = company.diario_defecto_pdf_id.id
                
            if not journal_id:
                raise UserError(_("No se puede crear la factura: Por favor configure los diarios por defecto (XML/PDF) en los ajustes de la compañía %s.") % company.name)

            # B. Cuenta Analítica por ciudad_prestacion (Prioridad)
            analitica = False
            ciudad_limpia = ""
            if rec.ciudad_prestacion:
                ciudad_limpia = rec.ciudad_prestacion.strip()
            elif rec.proveedor_id:
                if getattr(rec.proveedor_id, 'city_id', False):
                    ciudad_limpia = rec.proveedor_id.city_id.name.strip()
                elif getattr(rec.proveedor_id, 'city', False):
                    ciudad_limpia = rec.proveedor_id.city.strip()

            if ciudad_limpia:
                # Buscar cuenta analítica con operador ilike
                analitica = self.env['account.analytic.account'].search([('name', '=ilike', f'%{ciudad_limpia}%')], limit=1)
                if analitica:
                    _logger.info("Cuenta Analítica encontrada para ciudad '%s': %s (ID: %s)", ciudad_limpia, analitica.name, analitica.id)
                else:
                    _logger.warning("No se encontró cuenta analítica para la ciudad '%s'", ciudad_limpia)

            # Mapear líneas de factura desde las líneas extraídas (dian.invoice.line)
            lineas_factura = rec._construir_lineas_factura_desde_invoice_lines()
            
            # C. Distribución Binaria del IVA (Mayor Valor Gasto)
            nuevas_lineas = []
            for comando in lineas_factura:
                if comando[0] == 0:
                    vals = comando[2]
                    
                    # 1. Asignar Analítica si la cuenta empieza por '5'
                    if analitica:
                        cuenta_id = vals.get('account_id')
                        if cuenta_id:
                            cuenta = self.env['account.account'].browse(cuenta_id)
                            if cuenta.exists() and cuenta.code and cuenta.code.startswith('5'):
                                vals['analytic_account_id'] = analitica.id
                                _logger.info("Cuenta analítica %s asignada a línea con cuenta contable %s", analitica.name, cuenta.code)

                # Si no es un comando de creación o no aplica prorrateo, agregar tal cual (con o sin analítica)
                nuevas_lineas.append(comando)
            
            lineas_factura = nuevas_lineas

            # Armar referencia concatenando el número de factura y el servicio
            numero_factura = rec._get_numero_documento() or ''
            servicio = rec.servicio_id
            ref_str = f"{numero_factura} - {servicio.name}" if servicio else numero_factura

            # Lógica de Forma de Pago y Vencimiento Automático
            fecha_factura = rec._get_fecha_emision() or rec.fecha_efectiva or fields.Date.context_today(rec)
            invoice_date_due = fecha_factura
            forma_de_pago = '1' # Por defecto Contado
            payment_term_id = False
            
            if rec.proveedor_id.property_supplier_payment_term_id:
                payment_term = rec.proveedor_id.property_supplier_payment_term_id
                payment_term_id = payment_term.id
                # Buscar el plazo máximo de días en las líneas
                max_days = max([line.days for line in payment_term.line_ids], default=0)
                
                if max_days > 0:
                    forma_de_pago = '2' # Crédito
                    invoice_date_due = fecha_factura + timedelta(days=max_days)
            
            # Valores para crear el account.move
            move_vals = {
                "move_type": "in_invoice",
                "company_id": rec.compania_id.id,
                "partner_id": rec.proveedor_id.id,
                "journal_id": journal_id,
                "invoice_date": fecha_factura,
                "invoice_date_due": invoice_date_due,
                "invoice_payment_term_id": payment_term_id,
                "forma_de_pago": forma_de_pago,
                "ref": numero_factura,
                "payment_reference": ref_str,
                "invoice_line_ids": lineas_factura,
            }

            # Crear factura de proveedor
            move = self.env["account.move"].with_company(rec.compania_id.id).create(move_vals)
            
            # Relacionar factura con el extractor
            rec.factura_proveedor_id = move.id
            
            # Agregar la factura como adjunto/referencia al chatter
            rec.message_post(
                body=_("Factura de proveedor creada exitosamente: %s") % move.name,
            )

            return rec._action_abrir_factura(move)

    def _action_abrir_factura(self, move):
        return {
            "type": "ir.actions.act_window",
            "name": _("Factura proveedor"),
            "res_model": "account.move",
            "res_id": move.id,
            "view_mode": "form",
            "target": "current",
        }

    # -------------------------
    # Validaciones previas
    # -------------------------
    def _asegurar_listo_para_contabilizar(self):
        self.ensure_one()

        if self.bloqueado:
            raise UserError(_("Documento bloqueado: %s") % (self.motivo_bloqueo or ""))

        if not self.compania_id:
            raise UserError(_("Falta Compañía."))

        if not self.proveedor_id:
            raise UserError(_("Falta Proveedor."))

        if not self.servicio_id:
            raise UserError(_("Falta Servicio (maestro.servicios)."))

        if not self.es_xml and self.estado_ocr != "validado":
            raise UserError(_("Debe validar el OCR antes de crear la factura."))

        if not self.monto_documento:
            raise UserError(_("Falta monto (valor a pagar)."))

    # -------------------------
    # Lectura / detección archivo
    # -------------------------
    def _safe_b64decode(self, value):
        if not value:
            return None
        if isinstance(value, bytes):
            # si ya parece binario real (PDF/PNG/JPG), no lo decodifiques
            if value.startswith((b"%PDF", b"\x89PNG", b"\xff\xd8")):
                return value
            try:
                return base64.b64decode(value, validate=True)
            except Exception:
                try:
                    return base64.b64decode(value)
                except Exception:
                    return value
        if isinstance(value, str):
            return base64.b64decode(value)
        return value

    def _obtener_binario_y_nombre(self):
        self.ensure_one()
        filename = self.file_name or None
        data = None

        # modelo original: file_data siempre existe
        if "file_data" in self._fields and self.file_data:
            data = self._safe_b64decode(self.file_data)

        return data, filename

    def _parece_xml(self, data: bytes, filename: str):
        # Por extensión: nunca confundir PDF/imagenes con XML
        if filename and filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg")):
            return False

        head = (data or b"")[:2048].lstrip()

        # Firmas binarias
        if head.startswith(b"%PDF") or head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8"):
            return False

        low = head.lower()
        if low.startswith(b"<?xml"):
            return True

        # XML DIAN/UBL puede iniciar con <Invoice> o <AttachedDocument> etc.
        if low.startswith(b"<") and (b"<invoice" in low or b"<attacheddocument" in low or b"<creditnote" in low or b"<debitnote" in low):
            return True

        return False

    # -------------------------
    # Procesamiento XML
    # -------------------------
    def _procesar_xml_usando_extractor(self, data: bytes, filename: str):
        self.ensure_one()
        self.es_xml = True
        self.estado_ocr = "no_aplica"
        self.texto_ocr = False
        self.datos_ocr_json = False

        from lxml import etree
        _logger.info("Iniciando parseo de XML nativo para el documento ID: %s", self.id)
        lines_data = []
        
        try:
            root = etree.fromstring(data)
            
            # [2. EXTRACCIÓN DEL CDATA (BLINDADA)]
            _logger.info("Buscando nodo CDATA (AttachedDocument/Description)...")
            cdata_nodes = root.xpath("//*[local-name()='Description'][contains(text(), 'Invoice')]")
            if not cdata_nodes:
                cdata_nodes = root.xpath(".//*[local-name()='Attachment']//*[local-name()='Description']")
                
            if not cdata_nodes:
                _logger.warning("NO SE ENCONTRÓ CDATA EN EL XML")
                raise UserError(_("No se pudo extraer la factura del sobre DIAN (CDATA no encontrado)."))
                
            cdata_text = cdata_nodes[0].text
            _logger.info("CDATA encontrado con longitud: %s", len(cdata_text))
            
            root_factura = etree.fromstring(cdata_text.encode('utf-8'))
            
            # Extraer campos de cabecera desde la factura (dentro del CDATA)
            invoice_number = root_factura.xpath('//*[local-name()="ID"]/text()')
            if invoice_number:
                self.invoice_number = invoice_number[0]
                
            # [3. SECUENCIA ESTRICTA: PROVEEDOR -> AUTORIZACIÓN -> SERVICIO]
            supplier_nit = root_factura.xpath('//*[local-name()="AccountingSupplierParty"]//*[local-name()="PartyTaxScheme"]/*[local-name()="CompanyID"]/text()')
            if supplier_nit:
                self.supplier_nit = supplier_nit[0]
                _logger.info("Buscando proveedor con NIT: %s", self.supplier_nit)
                
                nit_prov = self._normalizar_identificacion(self.supplier_nit)
                if nit_prov:
                    prov = self._buscar_partner_por_identificacion(nit_prov)
                    if prov:
                        self.proveedor_id = prov.id
                        _logger.info("Proveedor encontrado: %s", prov.name)
                        
                        _logger.info("Buscando autorización vigente para proveedor %s...", prov.name)
                        auth = self.env['autorizacion.servicio'].search([
                            ('proveedor_id', '=', prov.id),
                            ('compania_id', '=', self.compania_id.id),
                            ('estado', '=', 'vigente')
                        ], limit=1)
                        
                        if auth:
                            _logger.info("Autorización vigente encontrada: %s", auth.display_name)
                            self.autorizacion_servicio_id = auth.id
                            self.servicio_id = auth.servicio_id.id
                        else:
                            _logger.info("No se encontró autorización vigente para el proveedor %s", prov.name)
                    else:
                        _logger.warning("Proveedor con NIT %s no encontrado en res.partner", self.supplier_nit)
                
            customer_nit = root_factura.xpath('//*[local-name()="AccountingCustomerParty"]//*[local-name()="PartyTaxScheme"]/*[local-name()="CompanyID"]/text()')
            if customer_nit:
                self.customer_nit = customer_nit[0]
                nit_cliente = self._normalizar_identificacion(self.customer_nit)
                if nit_cliente:
                    compania = self._buscar_compania_por_nit(nit_cliente)
                    if compania:
                        self.compania_id = compania.id
                
            issue_date = root_factura.xpath('//*[local-name()="IssueDate"]/text()')
            if issue_date:
                self.issue_date = issue_date[0]
                
            payable_amount = root_factura.xpath('//*[local-name()="LegalMonetaryTotal"]/*[local-name()="PayableAmount"]/text()')
            if payable_amount:
                self.payable_amount = payable_amount[0]
                
            # [4. CREACIÓN DE LÍNEAS DIAN]
            invoice_lines = root_factura.xpath('//*[local-name()="InvoiceLine"]')
            _logger.info("Líneas encontradas en el XML: %s", len(invoice_lines))
            
            for line in invoice_lines:
                desc = line.xpath('.//*[local-name()="Item"]/*[local-name()="Description"]/text()')
                amount = line.xpath('.//*[local-name()="LineExtensionAmount"]/text()')
                
                lines_data.append({
                    'descripcion': desc[0] if desc else '',
                    'valor_total_linea': float(amount[0]) if amount else 0.0
                })
                
        except Exception as e:
            _logger.error("Error en extracción XML: %s", str(e))
            raise UserError(_("Error procesando el XML: %s") % str(e))

        self.fecha_efectiva = self.invoice_period_end or self.issue_date or fields.Date.context_today(self)
        self.monto_documento = float(self.payable_amount or 0.0)

        # Llenar 'invoice_lines' desde la extracción nativa
        if lines_data:
            self._crear_lineas_desde_line_items(lines_data)

        # --- LÓGICA DE AUTOMATIZACIÓN XML -> FACTURA ---
        # 1. Propagar servicio_id a la cabecera si está vacío y alguna línea hizo Fuzzy Match
        if not self.servicio_id:
            linea_con_servicio = self.invoice_lines.filtered(lambda l: l.servicio_id)
            if linea_con_servicio:
                self.servicio_id = linea_con_servicio[0].servicio_id.id
                _logger.info("Auto-propagación XML: servicio_id %s asignado a la cabecera desde la línea.", self.servicio_id.name)

        # 2. Automatizar validación y creación de factura si los datos críticos están listos
        self._aplicar_reglas_asignacion_servicio()
        self._evaluar_bloqueo()
        
        if self.proveedor_id and self.servicio_id and not self.bloqueado:
            _logger.info("Automatización XML: Documento %s tiene proveedor y servicio. Intentando crear factura...", self.id)
            try:
                self.action_crear_factura_proveedor()
                _logger.info("Automatización XML: Factura creada exitosamente para el documento %s.", self.id)
            except Exception as e:
                _logger.error("Automatización XML falló al crear factura para el documento %s: %s", self.id, str(e))
                self._crear_actividad_si_aplica(_("Fallo en la automatización de la factura XML: %s") % str(e))
        else:
            self._crear_actividad_si_aplica(_("Revisar XML: Completar Proveedor/Servicio o revisar bloqueos."))

    # -------------------------
    # OCR
    # -------------------------
    def _parse_json_dict(self, value):
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except Exception:
                return {}
        return {}

    def _limpiar_texto_llm(self, text):
        if not text:
            return ""
        s = re.sub(r'!\[[^\]]*\]\([^)]+\)', ' ', text)
        s = re.sub(r'<[^>]+>', ' ', s)
        s = re.sub(r'\s+', ' ', s)
        return s.strip()

    def _buscar_regla_llm(self):
        """DEPRECATED: El prompt ahora es universal."""
        return True

    def _procesar_ocr(self, data: bytes, filename: str):
        self.ensure_one()
        self.es_xml = False
        self.estado_ocr = "pendiente"

        if not self.env.registry.get("transcriptor.ocr"):
            raise UserError(_("No existe el modelo transcriptor.ocr o el módulo OCR no está instalado."))

        # Crear documento OCR con company_id para que encuentre la regla correcta
        t_inicio_ocr = time.time()
        ocr_vals = {
            "name": filename or _("Documento OCR"),
            "original_file": base64.b64encode(data).decode('utf-8'),
            "file_name": filename or False,
        }
        
        # Pasar company_id si ya está asignado
        if self.compania_id:
            ocr_vals["company_id"] = self.compania_id.id
        
        ocr_doc = self.env["transcriptor.ocr"].create(ocr_vals)
        
        # Ejecutar OCR + LLM (esto ya busca la regla y extrae con LLM)
        res_action = ocr_doc.action_procesar_documento()
        
        _logger.info(
            "OCR terminado para extractor %s en %.2fs",
            self.id,
            time.time() - t_inicio_ocr,
        )

        # Obtener resultados ya procesados por transcriptor.ocr
        self.texto_ocr = self._limpiar_texto_llm(ocr_doc.raw_text or "")
        extracted_raw = ocr_doc.extracted_data or "{}"

        # Guardar JSON extraído
        self.datos_ocr_json = extracted_raw if isinstance(extracted_raw, str) else json.dumps(extracted_raw, ensure_ascii=False, indent=2)

        # Parsear JSON
        extracted_data = self._parse_json_dict(extracted_raw)
        datos_mapeados = self._mapear_datos_llm_a_campos(extracted_data)
        
        _logger.info(
            "Datos extraídos para extractor %s: claves JSON=%s, datos_mapeados=%s",
            self.id,
            list(extracted_data.keys()) if isinstance(extracted_data, dict) else type(extracted_data),
            list(datos_mapeados.keys()),
        )

        # Copiar proveedor_id desde transcriptor.ocr si fue detectado
        if ocr_doc.proveedor_id and not self.proveedor_id:
            self.proveedor_id = ocr_doc.proveedor_id.id
            _logger.info(
                "Proveedor copiado desde transcriptor.ocr %s: %s",
                ocr_doc.id,
                ocr_doc.proveedor_id.name,
            )

        # Asignar compañía usando NIT del cliente
        nit_cliente = datos_mapeados.get('nit_cliente')
        if nit_cliente and not self.compania_id:
            company = self._buscar_compania_por_nit(nit_cliente)
            if company:
                self.compania_id = company.id

        # Fallback: buscar proveedor por NIT si no fue detectado
        if not self.proveedor_id:
            nit_proveedor = datos_mapeados.get('nit_proveedor')
            if nit_proveedor:
                partner = self._buscar_partner_por_identificacion(nit_proveedor)
                if partner:
                    self.proveedor_id = partner.id

        # Fallback: buscar por nombre de proveedor
        if not self.proveedor_id and datos_mapeados.get('nombre_proveedor'):
            partner = self.env['res.partner'].search([
                ('name', 'ilike', datos_mapeados['nombre_proveedor']),
                ('company_type', '=', 'company')
            ], limit=1)
            if partner:
                self.proveedor_id = partner.id

        # Fallback: regex NIT en texto OCR
        if not self.proveedor_id and self.texto_ocr:
            m = re.search(r'\bNIT[:\s]*([0-9\.\-]+)', self.texto_ocr, re.IGNORECASE)
            if m:
                nit_fallback = re.sub(r'\D', '', m.group(1))
                nit_fallback = nit_fallback[:9] if len(nit_fallback) > 9 else nit_fallback
                if nit_fallback:
                    partner = self._buscar_partner_por_identificacion(nit_fallback)
                    if partner:
                        self.proveedor_id = partner.id
                        _logger.info(
                            "Proveedor asignado por regex NIT en extractor %s: %s (%s)",
                            self.id,
                            partner.id,
                            nit_fallback,
                        )

        # Asignar campos básicos desde datos mapeados
        if datos_mapeados.get('fecha_efectiva'):
            self.fecha_efectiva = datos_mapeados['fecha_efectiva']
        if extracted_data.get('total_a_pagar') is not None:
            self.monto_documento = self._parse_money(extracted_data['total_a_pagar'])
        elif datos_mapeados.get('monto_documento') is not None:
            self.monto_documento = datos_mapeados['monto_documento']
        if datos_mapeados.get('invoice_number'):
            self.invoice_number = datos_mapeados['invoice_number']
            
        # Asignar ciudad_prestacion extraída del JSON para uso posterior
        if extracted_data.get('ciudad_prestacion'):
            self.ciudad_prestacion = extracted_data.get('ciudad_prestacion')

        # Generar líneas de factura desde line_items
        if extracted_data.get('line_items'):
            self._crear_lineas_desde_line_items(extracted_data['line_items'])
        else:
            # Fallback al método tradicional
            self._generar_invoice_lines_desde_ocr()

        # Si aún no hay líneas pero hay monto, crear una línea genérica
        if not self.invoice_lines and self.monto_documento:
            self._crear_linea_generica()

        # --- LÓGICA DE AUTOMATIZACIÓN OCR -> FACTURA ---
        # 1. Propagar servicio_id a la cabecera si está vacío y alguna línea hizo Fuzzy Match
        if not self.servicio_id:
            linea_con_servicio = self.invoice_lines.filtered(lambda l: l.servicio_id)
            if linea_con_servicio:
                self.servicio_id = linea_con_servicio[0].servicio_id.id
                _logger.info("Auto-propagación: servicio_id %s asignado a la cabecera desde la línea.", self.servicio_id.name)

        # 2. Automatizar validación y creación de factura si los datos críticos están listos
        if self.proveedor_id and self.servicio_id:
            _logger.info("Automatización OCR: Documento %s tiene proveedor y servicio. Intentando validar y crear factura...", self.id)
            try:
                self.action_validar_ocr()
                # Omitimos el return de la acción de vista de la factura para no interrumpir el flujo backend
                self.action_crear_factura_proveedor()
                _logger.info("Automatización OCR: Factura creada exitosamente para el documento %s.", self.id)
            except Exception as e:
                _logger.error("Automatización OCR falló al crear factura para el documento %s: %s", self.id, str(e))
                self._crear_actividad_si_aplica(_("Fallo en la automatización de la factura: %s") % str(e))
        else:
            # Crear actividad para que el usuario revise si faltan datos clave
            self._crear_actividad_si_aplica(_("Validar OCR y completar Proveedor/Servicio/Compañía."))

        return res_action

    # -------------------------
    # Bloqueo por autorización + datos maestros
    # -------------------------
    def _evaluar_bloqueo(self):
        for rec in self:
            faltantes = []

            if not rec.compania_id:
                faltantes.append("Compañía")
            if not rec.proveedor_id:
                faltantes.append("Proveedor")
            if not rec.servicio_id:
                faltantes.append("Servicio")
            if not rec.fecha_efectiva:
                faltantes.append("Fecha efectiva")
            if not rec.monto_documento:
                faltantes.append("Monto (valor a pagar)")

            if not rec.es_xml and rec.estado_ocr != "validado":
                faltantes.append("Validación OCR")

            # Ciudad: acepta city_id o city (Char)
            tiene_ciudad = False
            if rec.proveedor_id:
                if "city_id" in rec.proveedor_id._fields and rec.proveedor_id.city_id:
                    tiene_ciudad = True
                elif (rec.proveedor_id.city or "").strip():
                    tiene_ciudad = True
            if not tiene_ciudad:
                faltantes.append("Ciudad del proveedor (configurar en Contactos)")

            if faltantes:
                rec.bloqueado = True
                rec.autorizacion_servicio_id = False
                rec.motivo_bloqueo = _("Faltan datos: %s") % ", ".join(faltantes)
                rec._crear_actividad_si_aplica(rec.motivo_bloqueo)
                continue

            autorizacion = rec._buscar_autorizacion_vigente(
                compania_id=rec.compania_id.id,
                proveedor_id=rec.proveedor_id.id,
                servicio_id=rec.servicio_id.id,
                fecha=rec.fecha_efectiva,
            )

            if not autorizacion:
                rec.bloqueado = True
                rec.autorizacion_servicio_id = False
                rec.motivo_bloqueo = _("No existe autorización vigente para este proveedor/servicio en esa fecha.")
                rec._crear_actividad_si_aplica(rec.motivo_bloqueo)
                continue

            monto_aut = autorizacion.monto_mensual_fijo or 0.0
            if monto_aut and abs((rec.monto_documento or 0.0) - monto_aut) > 0.01:
                rec.bloqueado = True
                rec.autorizacion_servicio_id = autorizacion.id
                rec.motivo_bloqueo = _(
                    "Monto no coincide con monto fijo autorizado. Documento: %(doc)s / Autorizado: %(aut)s"
                ) % {"doc": rec.monto_documento, "aut": monto_aut}
                rec._crear_actividad_si_aplica(rec.motivo_bloqueo)
                continue

            rec.bloqueado = False
            rec.autorizacion_servicio_id = autorizacion.id
            rec.motivo_bloqueo = False

    def _buscar_autorizacion_vigente(self, compania_id, proveedor_id, servicio_id, fecha):
        Aut = self.env["autorizacion.servicio"]
        domain = [
            ("compania_id", "=", compania_id),
            ("proveedor_id", "=", proveedor_id),
            ("servicio_id", "=", servicio_id),
            ("fecha_inicio", "<=", fecha),
            ("fecha_fin", ">=", fecha),
            ("estado", "=", "vigente"),
        ]

        # Si hay city_id estructurado, permite autorización sin ciudad o con la misma
        city_id = self._get_ciudad_proveedor_id()
        if city_id:
            domain += ["|", ("ciudad_id", "=", False), ("ciudad_id", "=", city_id)]

        return Aut.search(domain, limit=1)

    # -------------------------
    # Construcción factura proveedor
    # -------------------------
    # -------------------------
    # TODO lo demás: dejo tu código intacto debajo (helpers, líneas, etc.)
    # -------------------------

    def _obtener_linea_configuracion_servicio(self):
        self.ensure_one()
        servicio = self.servicio_id

        line_field = self._find_first_field(servicio, ["line_ids", "linea_ids", "servicio_line_ids", "servicios_line_ids", "linea_exclusion_ids"])
        if not line_field:
            raise UserError(_("No encontré el One2many de líneas en maestro.servicios."))

        lineas = getattr(servicio, line_field)
        if not lineas:
            raise UserError(_("El servicio no tiene líneas de configuración."))

        # filtrar por company si existe en la línea (y usar compania_id)
        if self._field_exists(lineas, "company_id"):
            lineas = lineas.filtered(lambda l: (not l.company_id) or (l.company_id.id == self.compania_id.id)) or lineas

        # filtrar por ciudad si existe
        ciudad_id = self._get_ciudad_proveedor_id()
        for city_field in ("city_id", "ciudad_id"):
            if self._field_exists(lineas, city_field) and ciudad_id:
                lineas_ciudad = lineas.filtered(lambda l: getattr(l, city_field).id == ciudad_id)
                if lineas_ciudad:
                    lineas = lineas_ciudad
                    break

        return lineas[:1]

    # ----- (aquí sigue tu mismo bloque de métodos tal como lo tenías) -----

    def _construir_lineas_factura(self, linea_cfg):
        # tu implementación actual
        cuenta_id = self._get_cuenta_gasto_id(linea_cfg)
        impuestos_ids = []
        if self._field_exists(linea_cfg, "grupo_impuestos") and linea_cfg.grupo_impuestos:
            impuestos_ids = linea_cfg.grupo_impuestos.ids
        analytic_id = self._get_analytic_id(linea_cfg)

        extracted_lines = self._get_extracted_lines()
        commands = []

        if extracted_lines:
            for l in extracted_lines:
                nombre = self._get_line_value(l, ["description", "name", "concept", "producto", "item_name"]) or self.servicio_id.display_name
                qty = self._to_float(self._get_line_value(l, ["quantity", "qty", "cantidad"])) or 1.0
                price_unit = self._to_float(self._get_line_value(l, ["price_unit", "unit_price", "valor_unitario", "unitValue"]))
                subtotal = self._to_float(self._get_line_value(l, ["subtotal", "price_subtotal", "line_extension_amount", "base"]))
                if not price_unit:
                    if subtotal and qty:
                        price_unit = subtotal / qty
                    else:
                        price_unit = (self.monto_documento or 0.0) / qty

                vals = {
                    "name": nombre,
                    "quantity": qty,
                    "price_unit": price_unit,
                    "account_id": cuenta_id,
                }
                if impuestos_ids:
                    vals["tax_ids"] = [(6, 0, impuestos_ids)]
                if analytic_id:
                    vals["analytic_account_id"] = analytic_id
                commands.append((0, 0, vals))
        else:
            vals = {
                "name": self.servicio_id.display_name,
                "quantity": 1.0,
                "price_unit": self.monto_documento,
                "account_id": cuenta_id,
            }
            if impuestos_ids:
                vals["tax_ids"] = [(6, 0, impuestos_ids)]
            if analytic_id:
                vals["analytic_account_id"] = analytic_id
            commands.append((0, 0, vals))

        return commands

    def _get_extracted_lines(self):
        self.ensure_one()
        for fname in ["line_ids", "invoice_line_ids", "lines", "detalle_ids", "item_ids", "invoice_lines"]:
            if fname in self._fields:
                lines = getattr(self, fname)
                if lines:
                    return lines
        return False

    def _get_line_value(self, line, names):
        for n in names:
            if hasattr(line, n):
                return getattr(line, n)
        return False

    def _get_cuenta_gasto_id(self, linea_cfg):
        self.ensure_one()
        candidatos = [
            "account_id", "cuenta_id", "cuenta_gasto_id", "cuenta_gastos_id",
            "expense_account_id", "account_expense_id", "cuentas"
        ]
        for f in candidatos:
            if self._field_exists(linea_cfg, f):
                acc = getattr(linea_cfg, f)
                if acc:
                    return acc.id
        raise UserError(_("No encontré la cuenta de gasto en maestro.servicios.line."))

    def _get_analytic_id(self, linea_cfg):
        for f in ["analytic_account_id", "cuenta_analitica_id", "analytic_id"]:
            if self._field_exists(linea_cfg, f):
                a = getattr(linea_cfg, f)
                return a.id if a else False
        return False

    def _get_fecha_emision(self):
        return self._get_first_existing_value(["issue_date", "fecha_emision", "invoice_date"])

    def _get_numero_documento(self):
        return self._get_first_existing_value(["invoice_number", "numero", "number", "ref", "cufe", "uuid"])

    def _get_first_existing_value(self, field_names):
        for fn in field_names:
            if fn in self._fields:
                v = getattr(self, fn)
                if v:
                    return v
        return False

    def _get_ciudad_proveedor_id(self):
        self.ensure_one()
        p = self.proveedor_id
        if not p:
            return False
        if "city_id" in p._fields and p.city_id:
            return p.city_id.id
        return False

    def _normalizar_identificacion(self, value):
        if not value:
            return None
        digits = re.sub(r"\D+", "", str(value))
        if len(digits) >= 9:
            return digits[:9]
        return digits or None



    def _buscar_partner_por_identificacion(self, nit_9):
        if not nit_9:
            return self.env["res.partner"]
        
        domain_base = [('id', '!=', self.env.company.partner_id.id)]
        
        candidatos = self.env["res.partner"].search(domain_base + [("fe_nit", "ilike", nit_9)], limit=20)
        nit_9_norm = self._normalizar_identificacion(nit_9)
        for p in candidatos:
            if self._normalizar_identificacion(getattr(p, "fe_nit", "")) == nit_9_norm:
                return p

        candidatos = self.env["res.partner"].search(domain_base + [("vat", "ilike", nit_9)], limit=20)
        for p in candidatos:
            if self._normalizar_identificacion(getattr(p, "vat", "")) == nit_9_norm:
                return p

        return self.env["res.partner"]

    def _buscar_compania_por_nit(self, nit_9):
        if not nit_9:
            return self.env["res.company"]
        comps = self.env["res.company"].search([("partner_id.fe_nit", "ilike", nit_9)])
        nit_9 = self._normalizar_identificacion(nit_9)
        for c in comps:
            if self._normalizar_identificacion(getattr(c.partner_id, "fe_nit", "")) == nit_9:
                return c
        return self.env["res.company"]

    def _to_float(self, value):
        if value in (None, False, ""):
            return 0.0
        try:
            if isinstance(value, str):
                v = value.replace(".", "").replace(",", ".")
                return float(v)
            return float(value)
        except Exception:
            return 0.0

    def _field_exists(self, recordset_or_record, field_name):
        try:
            return field_name in recordset_or_record._fields
        except Exception:
            return False

    def _find_first_field(self, record, candidates):
        for c in candidates:
            if c in record._fields:
                return c
        return False

    def _crear_actividad_si_aplica(self, nota):
        self.ensure_one()
        try:
            actividad_tipo = self.env.ref("mail.mail_activity_data_todo")
        except Exception:
            return
        existente = self.env["mail.activity"].search([
            ("res_model", "=", self._name),
            ("res_id", "=", self.id),
            ("activity_type_id", "=", actividad_tipo.id),
            ("user_id", "=", self.env.user.id),
        ], limit=1)
        if existente:
            return
        self.activity_schedule(
            activity_type_id=actividad_tipo.id,
            user_id=self.env.user.id,
            summary=_("Pendiente"),
            note=nota or "",
        )



    def action_aplicar_reglas_servicio(self):
        for rec in self:
            rec._aplicar_reglas_asignacion_servicio()
            rec._evaluar_bloqueo()

    def _aplicar_reglas_asignacion_servicio(self):
        """
        Busca y asigna un servicio contable tanto a la cabecera como a las líneas 
        de la factura extraída basándose en el proveedor o la ciudad usando las reglas.
        """
        self.ensure_one()
        if not self.compania_id:
            return

        # 1. Asignación a nivel de Cabecera (Extractor)
        if not self.servicio_id:
            ciudad_id = False
            ciudad_texto = False
            
            if self.proveedor_id:
                if "city_id" in self.proveedor_id._fields and self.proveedor_id.city_id:
                    ciudad_id = self.proveedor_id.city_id.id
                ciudad_texto = (self.proveedor_id.city or "").upper()

            payload_cabecera = {
                "aplica_a": "documento",
                "company_id": self.compania_id.id,
                "proveedor_id": self.proveedor_id.id if self.proveedor_id else False,
                "ciudad_id": ciudad_id,
                "ciudad_texto": ciudad_texto,
                "texto_busqueda": (self.texto_ocr or "").strip(),
            }

            reglas_cabecera = self.env["regla.asignacion.servicio"].search([
                ("active", "=", True),
                ("company_id", "=", self.compania_id.id),
                ("aplica_a", "=", "documento"),
                "|", ("proveedor_id", "=", False), ("proveedor_id", "=", self.proveedor_id.id if self.proveedor_id else False),
            ], order="prioridad desc, id desc")

            for r in reglas_cabecera:
                if r.match(payload_cabecera):
                    self.servicio_id = r.servicio_id.id
                    self.message_post(body=_("Servicio de cabecera asignado por regla: %s") % r.name)
                    break

        # 2. Asignación a nivel de Líneas (dian.invoice.line)
        if not self.invoice_lines:
            # Si no hay líneas creadas, no hay nada que asignar.
            # En OCR esto debe dispararse después de crear las líneas genéricas.
            return

        reglas_linea = self.env["regla.asignacion.servicio"].search([
            ("active", "=", True),
            ("company_id", "=", self.compania_id.id),
            ("aplica_a", "=", "linea"),
            "|", ("proveedor_id", "=", False), ("proveedor_id", "=", self.proveedor_id.id if self.proveedor_id else False),
        ], order="prioridad desc, id desc")

        for line in self.invoice_lines:
            # Si ya tiene servicio, lo respetamos
            if line.servicio_id:
                continue

            payload_linea = {
                "aplica_a": "linea",
                "company_id": self.compania_id.id,
                "proveedor_id": self.proveedor_id.id if self.proveedor_id else False,
                "ciudad_id": self.proveedor_id.city_id.id if (self.proveedor_id and "city_id" in self.proveedor_id._fields and self.proveedor_id.city_id) else False,
                "ciudad_texto": (self.proveedor_id.city or "") if self.proveedor_id else "",
                "codigo_producto": (line.product_code or "").strip(),
                "texto_busqueda": (line.description or "").strip(),
            }

            asignado = False
            for r in reglas_linea:
                if r.match(payload_linea):
                    line.servicio_id = r.servicio_id.id
                    asignado = True
                    break
            
            # Fallback: Si no hay regla de línea que aplique, pero hay un servicio de cabecera,
            # se lo asignamos por defecto a la línea para no romper la facturación.
            if not asignado and self.servicio_id:
                line.servicio_id = self.servicio_id.id






    def action_asignar_servicios_lineas(self):
        for rec in self:
            # Si no hay líneas, intentar generarlas (solo OCR)
            if not rec.invoice_lines and not rec.es_xml:
                if rec.estado_ocr != "validado":
                    raise UserError(_("Debes validar el OCR antes de asignar servicios por línea."))
                rec._generar_invoice_lines_desde_ocr()

            if not rec.invoice_lines:
                raise UserError(_("No hay líneas DIAN en esta factura."))


            reglas = rec.env["regla.asignacion.servicio"].search([
                ("active", "=", True),
                ("company_id", "=", rec.compania_id.id),
                ("aplica_a", "=", "linea"),
                "|", ("proveedor_id", "=", False), ("proveedor_id", "=", rec.proveedor_id.id),
            ], order="prioridad desc, id desc")

            for line in rec.invoice_lines:
                if line.servicio_id:
                    continue

                payload = {
                    "aplica_a": "linea",
                    "company_id": rec.compania_id.id,
                    "proveedor_id": rec.proveedor_id.id if rec.proveedor_id else False,
                    # "tipo_documento": "xml" if rec.es_xml else "otro",
                    "ciudad_id": rec.proveedor_id.city_id.id if (rec.proveedor_id and "city_id" in rec.proveedor_id._fields and rec.proveedor_id.city_id) else False,
                    "ciudad_texto": (rec.proveedor_id.city or "") if rec.proveedor_id else "",
                    "codigo_producto": (line.product_code or "").strip(),
                    "texto_busqueda": (line.description or ""),
                }

                asignado = False
                for r in reglas:
                    if r.match(payload):
                        line.servicio_id = r.servicio_id.id
                        asignado = True
                        break
                if not asignado and rec.servicio_id:
                    line.servicio_id = rec.servicio_id.id
                    
                    
                    
                    
    def _parse_money(self, value):
        """
        Convierte cualquier string de moneda o porcentaje devuelto por el OCR en un float válido.
        Maneja formatos latinos y americanos, remueve símbolos ($, %, COP, etc) y espacios.
        """
        if value is None or value == "":
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        try:
            # 1. Limpiar todo lo que no sea dígito, punto, coma o signo negativo
            s = str(value)
            s = re.sub(r'[^\d\.,\-]', '', s).strip()
            
            if not s:
                return 0.0

            # 2. Encontrar el último separador (. o ,)
            last_dot = s.rfind('.')
            last_comma = s.rfind(',')
            last_separator_idx = max(last_dot, last_comma)

            # 3. Lógica heurística para determinar si el último separador es decimal
            is_decimal = False
            if last_separator_idx != -1:
                # Si el separador está a 1, 2 o 3 posiciones del final, lo tratamos como decimal
                # (Ej: 3,50 / 3.500 / 19,5)
                # OJO: Si es exactamente 3 dígitos, podría ser miles (ej: 1.000).
                # Para evitar ese error, si hay OTRO separador antes, o si la longitud decimal no es 3, es decimal.
                chars_after = len(s) - last_separator_idx - 1
                if chars_after in (1, 2):
                    is_decimal = True
                elif chars_after == 3:
                    # Caso ambiguo: "1.000" vs "1,000". Si el separador es coma y no hay puntos, suele ser miles en latam o decimal en US.
                    # Verificamos si hay otro separador del mismo tipo antes (ej: 1,000,000)
                    if s.count(s[last_separator_idx]) > 1:
                        is_decimal = False # Es un separador de miles repetido
                    # Si hay punto y coma (ej: 1,000.000 o 1.000,000), el último manda
                    elif last_dot != -1 and last_comma != -1:
                        is_decimal = True
                    else:
                        # Por defecto asumimos que un solo punto/coma con 3 dígitos es separador de miles
                        is_decimal = False
                else:
                    # Más de 3 decimales? Raro en facturas, pero lo asumimos decimal
                    is_decimal = True

            # 4. Formatear el string para float() de Python
            if is_decimal:
                # Extraer la parte entera y decimal
                integer_part = s[:last_separator_idx]
                decimal_part = s[last_separator_idx + 1:]
                # Limpiar cualquier separador que haya quedado en la parte entera
                integer_part = integer_part.replace('.', '').replace(',', '')
                s_final = f"{integer_part}.{decimal_part}"
            else:
                # No hay decimales, limpiar todo punto y coma
                s_final = s.replace('.', '').replace(',', '')

            return float(s_final)
        except Exception as e:
            _logger.warning("Error en _parse_money al convertir '%s': %s", value, str(e))
            return 0.0

    def _is_percent(self, token):
        t = token.strip().replace("%", "")
        return t.isdigit()

    def _is_number(self, token):
        t = token.strip().replace(".", "").replace(",", "")
        return t.isdigit()

    def _extraer_items_tabla_desde_texto(self, raw_text):
        """
        Parser específico para facturas tipo Faster:
        CODIGO  C.C.  DESCRIPCION  CANTIDAD  %IVA  VALOR TOTAL
        """
        if not raw_text:
            return []

        lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
        items = []
        current = None

        for l in lines:
            m = CODIGO_RE.match(l)
            if m:
                # guarda el anterior
                if current:
                    items.append(current)  
                current = {
                    "product_code": m.group("code"),
                    "cc": m.group("cc") or "",
                    "raw": m.group("body"),
                }
                
            else:
                # continuación de descripción (ej. última línea “Almacenamiento de Enero…”)
                if current:
                    current["raw"] += " " + l

        if current:
            items.append(current)

        parsed = []
        for it in items:
            tokens = it["raw"].split()
            if len(tokens) < 4:
                continue

            # valor total suele ser el último token monetario
            valor_total = self._parse_money(tokens[-1])

            # iva% suele ser penúltimo token (19% o 19)
            iva_token = tokens[-2]
            iva_pct = float(iva_token.replace("%", "")) if self._is_percent(iva_token) else 0.0

            # cantidad suele ser el token antes del iva
            qty_token = tokens[-3]
            qty = float(qty_token.replace(",", ".")) if self._is_number(qty_token) else 1.0

            # descripción: lo que queda entre tokens[0: -3]
            desc = " ".join(tokens[:-3]).strip()

            # Si el valor total incluye IVA (como en Faster), calculamos base e impuesto
            base = valor_total
            tax_amount = 0.0
            if iva_pct > 0:
                base = round(valor_total / (1.0 + iva_pct / 100.0), 2)
                tax_amount = round(valor_total - base, 2)

            parsed.append({
                "product_code": it["product_code"],
                "description": desc,
                "quantity": qty,
                "tax_percent": iva_pct,
                "line_extension_amount": base,   # base sin IVA
                "tax_amount": tax_amount,
                "tax_scheme": "IVA" if iva_pct else "",
            })

        return parsed


    def _generar_invoice_lines_desde_ocr(self):
        self.ensure_one()
        if self.es_xml:
            return

        # Intentar obtener line_items del JSON extraído
        line_items = []
        if self.datos_ocr_json:
            try:
                datos = json.loads(self.datos_ocr_json)
                line_items = datos.get('line_items', [])
            except:
                pass

        if line_items:
            valido = False
            for it in line_items:
                if any(it.get(k) for k in ("codigo", "descripcion", "cantidad", "valor_total_linea")):
                    valido = True
                    break
            if not valido:
                line_items = []

        if line_items:
            # Usar los ítems proporcionados por el LLM
            self.invoice_lines.unlink()
            Line = self.env["dian.invoice.line"]
            seq = 1
            
            # Cargar etiquetas en memoria para Fuzzy Matching
            etiquetas = self.env['maestro.servicios.etiqueta'].search([])

            for it in line_items:          
                qty = self._parse_money(it.get('cantidad', 1.0))
                base = self._parse_money(it.get('valor_total_linea', 0.0))
                iva_pct = self._parse_money(it.get('porcentaje_iva', 0.0))
                # La base ya viene sin IVA en el valor extraído de las facturas de proveedores
                base_sin_iva = base
                
                # Calcular monto de impuesto
                if iva_pct > 0:
                    tax_amount = base_sin_iva * (iva_pct / 100.0)
                else:
                    tax_amount = 0.0

                # Precio unitario (evitar división por cero)
                price_unit = base_sin_iva / qty if qty else base_sin_iva

                descripcion = it.get('descripcion', '').strip()
                
                # Fuzzy Matching para asignar servicio
                servicio_asignado_id = False
                if descripcion:
                    mejor_ratio = 0.0
                    mejor_etiqueta = None
                    for etiqueta in etiquetas:
                        if not etiqueta.name:
                            continue
                        ratio = SequenceMatcher(None, descripcion.lower(), etiqueta.name.lower()).ratio()
                        if ratio > mejor_ratio:
                            mejor_ratio = ratio
                            mejor_etiqueta = etiqueta

                    if mejor_ratio >= 0.90 and mejor_etiqueta:
                        servicio_asignado_id = mejor_etiqueta.servicio_id.id
                        _logger.info("OCR Fuzzy Match: Línea '%s' asignada al servicio '%s' (Ratio: %.2f%%)", descripcion, mejor_etiqueta.servicio_id.name, mejor_ratio * 100)
                    else:
                        _logger.info("OCR Fuzzy Match: Línea '%s' sin coincidencia suficiente (Mejor ratio: %.2f%%)", descripcion, mejor_ratio * 100)

                # Fallback
                if not servicio_asignado_id:
                    servicio_asignado_id = self.servicio_id.id if self.servicio_id else False

                Line.create({
                    'invoice_id': self.id,
                    'sequence': seq,
                    'product_code': it.get('codigo', ''),
                    'description': descripcion,
                    'quantity': qty,
                    'price_unit': round(price_unit, 6),
                    'line_extension_amount': round(base_sin_iva, 2),
                    'tax_amount': round(tax_amount, 2),
                    'tax_percent': iva_pct,
                    'tax_scheme': 'IVA' if iva_pct else '',
                    'servicio_id': servicio_asignado_id,
                })
                seq += 1
        else:
            # Si no hay line_items, usar el parseo tradicional (regex)
            raw = self.texto_ocr or ""
            items = self._extraer_items_tabla_desde_texto(raw)
            # ... (código existente para crear líneas)




prueba con pdf faster
Visor de Logs
▶
🗑️
🌙
2026-04-27 13:42:57,605 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:42:57] "POST /web/dataset/call_kw/maestro.servicios/load_views HTTP/1.0" 200 - 43 0.037 0.030
2026-04-27 13:42:57,624 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
2026-04-27 13:42:57,748 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:42:57] "POST /web/dataset/search_read HTTP/1.0" 200 - 5 0.115 0.011
2026-04-27 13:42:58,347 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
2026-04-27 13:42:58,355 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:42:58] "POST /web/dataset/search_read HTTP/1.0" 200 - 3 0.002 0.007
2026-04-27 13:42:59,433 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
2026-04-27 13:42:59,437 1473585 DEBUG dismel odoo.api: call dian.invoice.extractor().onchange({}, [], {'state': '', 'total_lines': '', 'payable_amount': '', 'invoice_number': '', 'supplier_name': '', 'customer_name': '', 'cufe': '', 'invoice_type_code': '', 'invoice_uuid': '', 'issue_date': '', 'issue_time': '', 'due_date': '', 'currency_code': '', 'invoice_period_start': '', 'invoice_period_end': '', 'upload_date': '', 'supplier_nit': '', 'supplier_company_id': '', 'supplier_tax_level': '', 'supplier_address': '', 'supplier_city': '', 'supplier_department': '', 'supplier_phone': '', 'supplier_email': '', 'customer_nit': '', 'customer_company_id': '', 'customer_tax_level': '', 'customer_address': '', 'customer_city': '', 'customer_department': '', 'customer_phone': '', 'customer_email': '', 'dian_authorization': '', 'dian_authorization_start': '', 'dian_authorization_end': '', 'dian_prefix': '', 'dian_from': '', 'dian_to': '', 'software_provider_id': '', 'software_id': '', 'dian_response_code': '', 'dian_validation_date': '', 'dian_validation_time': '', 'dian_response_description': '', 'qr_code': '', 'line_extension_amount': '', 'tax_exclusive_amount': '', 'tax_inclusive_amount': '', 'allowance_total_amount': '', 'charge_total_amount': '', 'prepaid_amount': '', 'total_tax_amount': '', 'total_iva': '', 'total_rete_fuente': '', 'total_rete_iva': '', 'total_rete_ica': '', 'total_quantity': '', 'total_value': '', 'payment_means_code': '', 'payment_id': '', 'payment_due_date': '', 'purchase_order': '', 'dispatch_document': '', 'receipt_document': '', 'additional_document_ref': '', 'file_name': '', 'file_data': '', 'processing_log': '', 'error_message': '', 'factura_proveedor_id': '', 'monto_documento': '', 'fecha_efectiva': '', 'bloqueado': '', 'motivo_bloqueo': '', 'es_xml': '', 'estado_ocr': '', 'compania_id': '', 'proveedor_id': '', 'servicio_id': '', 'currency_id': '', 'autorizacion_servicio_id': '', 'texto_ocr': '', 'datos_ocr_json': '', 'invoice_lines': '1', 'invoice_lines.sequence': '', 'invoice_lines.product_code': '', 'invoice_lines.description': '', 'invoice_lines.quantity': '1', 'invoice_lines.tax_percent': '', 'invoice_lines.line_extension_amount': '1', 'invoice_lines.servicio_id': ''}) 
2026-04-27 13:42:59,564 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:42:59] "POST /web/dataset/call_kw/dian.invoice.extractor/onchange HTTP/1.0" 200 - 10 0.109 0.023
2026-04-27 13:42:59,881 1473585 INFO ? werkzeug: 127.0.0.1 - - [27/Apr/2026 13:42:59] "GET /web_enterprise/static/src/img/down-arrow.png HTTP/1.0" 304 - - - -
2026-04-27 13:43:00,820 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:00] "POST /mail/init_messaging HTTP/1.0" 200 - 144 840.688 0.754
[heartbeat lun abr 27 08:43:04 -05 2026]
[heartbeat lun abr 27 08:43:06 -05 2026]
[heartbeat lun abr 27 08:43:08 -05 2026]
[heartbeat lun abr 27 08:43:10 -05 2026]
2026-04-27 13:43:10,888 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
2026-04-27 13:43:10,892 1473585 DEBUG dismel odoo.api: call dian.invoice.extractor().create({'state': 'draft', 'payable_amount': 0, 'invoice_number': False, 'supplier_name': False, 'customer_name': False, 'cufe': False, 'invoice_type_code': False, 'invoice_uuid': False, 'issue_date': False, 'issue_time': False, 'due_date': False, 'currency_code': 'COP', 'invoice_period_start': False, 'invoice_period_end': False, 'upload_date': '2026-04-27 13:42:59', 'supplier_nit': False, 'supplier_company_id': False, 'supplier_tax_level': False, 'supplier_address': False, 'supplier_city': False, 'supplier_department': False, 'supplier_phone': False, 'supplier_email': False, 'customer_nit': False, 'customer_company_id': False, 'customer_tax_level': False, 'customer_address': False, 'customer_city': False, 'customer_department': False, 'customer_phone': False, 'customer_email': False, 'dian_authorization': False, 'dian_authorization_start': False, 'dian_authorization_end': False, 'dian_prefix': False, 'dian_from': False, 'dian_to': False, 'software_provider_id': False, 'software_id': False, 'dian_response_code': False, 'dian_validation_date': False, 'dian_validation_time': False, 'dian_response_description': False, 'qr_code': False, 'line_extension_amount': 0, 'tax_exclusive_amount': 0, 'tax_inclusive_amount': 0, 'allowance_total_amount': 0, 'charge_total_amount': 0, 'prepaid_amount': 0, 'total_tax_amount': 0, 'total_iva': 0, 'total_rete_fuente': 0, 'total_rete_iva': 0, 'total_rete_ica': 0, 'payment_means_code': False, 'payment_id': False, 'payment_due_date': False, 'purchase_order': False, 'dispatch_document': False, 'receipt_document': False, 'additional_document_ref': False, 'file_name': 'fv09007947870212600011510.pdf', 'file_data': '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Nzc4LjAgNjY3LjAgNzc4LjAgNzIyLjAgNjY3LjAgNjExLjAgNzIyLjAgNjY3LjAgOTQ0LjAgNjY3LjAgNjY3LjAgNjExLjAgMzMzLjAgMjc4LjAgMzMzLjAgNTg0LjAgNTU2LjAgMzMzLjAgNTU2LjAgNjExLjAgNTU2LjAgNjExLjAgNTU2LjAgMzMzLjAgNjExLjAgNjExLjAgMjc4LjAgMjc4LjAgNTU2LjAgMjc4LjAgODg5LjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgMzg5LjAgNTU2LjAgMzMzLjAgNjExLjAgNTU2LjAgNzc4LjAgNTU2LjAgNTU2LjAgNTAwLjAgMzg5LjAgMjgwLjAgMzg5LjAgNTg0LjAgNzUwLjAgNTU2LjAgNzUwLjAgMjc4LjAgNTU2LjAgNTAwLjAgMTAwMC4wIDU1Ni4wIDU1Ni4wIDMzMy4wIDEwMDAuMCA2NjcuMCAzMzMuMCAxMDAwLjAgNzUwLjAgNjExLjAgNzUwLjAgNzUwLjAgMjc4LjAgMjc4LjAgNTAwLjAgNTAwLjAgMzUwLjAgNTU2LjAgMTAwMC4wIDMzMy4wIDEwMDAuMCA1NTYuMCAzMzMuMCA5NDQuMCA3NTAuMCA1MDAuMCA2NjcuMCAyNzguMCAzMzMuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCAyODAuMCA1NTYuMCAzMzMuMCA3MzcuMCAzNzAuMCA1NTYuMCA1ODQuMCAzMzMuMCA3MzcuMCA1NTIuMCA0MDAuMCA1NDkuMCAzMzMuMCAzMzMuMCAzMzMuMCA1NzYuMCA1NTYuMCAzMzMuMCAzMzMuMCAzMzMuMCAzNjUuMCA1NTYuMCA4MzQuMCA4MzQuMCA4MzQuMCA2MTEuMCA3MjIuMCA3MjIuMCA3MjIuMCA3MjIuMCA3MjIuMCA3MjIuMCAxMDAwLjAgNzIyLjAgNjY3LjAgNjY3LjAgNjY3LjAgNjY3LjAgMjc4LjAgMjc4LjAgMjc4LjAgMjc4LjAgNzIyLjAgNzIyLjAgNzc4LjAgNzc4LjAgNzc4LjAgNzc4LjAgNzc4LjAgNTg0LjAgNzc4LjAgNzIyLjAgNzIyLjAgNzIyLjAgNzIyLjAgNjY3LjAgNjY3LjAgNjExLjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgODg5LjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgMjc4LjAgMjc4LjAgMjc4LjAgMjc4LjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNTQ5LjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNTU2LjAgNjExLjAgNTU2LjAgDQpdDQplbmRvYmoNCjcgMCBvYmoNCjw8IC9UeXBlIC9FbmNvZGluZw0KL0Jhc2VFbmNvZGluZyAvV2luQW5zaUVuY29kaW5nDQo+Pg0KZW5kb2JqDQoxMCAwIG9iag0KPDwgL1R5cGUgL0ZvbnQNCi9TdWJ0eXBlIC9UcnVlVHlwZQ0KL05hbWUgL0FyaWFsDQovQmFzZUZvbnQgL0FyaWFsDQovRmlyc3RDaGFyIDMwDQovTGFzdENoYXIgMjU1DQovRm9udERlc2NyaXB0b3IgMTIgMCBSDQovV2lkdGhzIDEzIDAgUg0KL0VuY29kaW5nIDExIDAgUg0KPj4NCmVuZG9iag0KMTIgMCBvYmoNCjw8IC9UeXBlIC9Gb250RGVzY3JpcHRvcg0KL0ZvbnROYW1lIC9BcmlhbA0KL0FzY2VudCA5MDUuMA0KL0NhcEhlaWdodCA5MDUuMA0KL0Rlc2NlbnQgLTIxMi4wDQovRmxhZ3MgMzINCi9Gb250QkJveCBbIC0yNTAuMCAtMjEyLjAgMjY2NS4wIDkwNS4wIF0NCi9JdGFsaWNBbmdsZSAwDQovU3RlbVYgMA0KPj4NCmVuZG9iag0KMTMgMCBvYmoNClsNCjc1MC4wIDc1MC4wIDI3OC4wIDI3OC4wIDM1NS4wIDU1Ni4wIDU1Ni4wIDg4OS4wIDY2Ny4wIDE5MS4wIDMzMy4wIDMzMy4wIDM4OS4wIDU4NC4wIDI3OC4wIDMzMy4wIDI3OC4wIDI3OC4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDI3OC4wIDI3OC4wIDU4NC4wIDU4NC4wIDU4NC4wIDU1Ni4wIDEwMTUuMCA2NjcuMCA2NjcuMCA3MjIuMCA3MjIuMCA2NjcuMCA2MTEuMCA3NzguMCA3MjIuMCAyNzguMCA1MDAuMCA2NjcuMCA1NTYuMCA4MzMuMCA3MjIuMCA3NzguMCA2NjcuMCA3NzguMCA3MjIuMCA2NjcuMCA2MTEuMCA3MjIuMCA2NjcuMCA5NDQuMCA2NjcuMCA2NjcuMCA2MTEuMCAyNzguMCAyNzguMCAyNzguMCA0NjkuMCA1NTYuMCAzMzMuMCA1NTYuMCA1NTYuMCA1MDAuMCA1NTYuMCA1NTYuMCAyNzguMCA1NTYuMCA1NTYuMCAyMjIuMCAyMjIuMCA1MDAuMCAyMjIuMCA4MzMuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCAzMzMuMCA1MDAuMCAyNzguMCA1NTYuMCA1MDAuMCA3MjIuMCA1MDAuMCA1MDAuMCA1MDAuMCAzMzQuMCAyNjAuMCAzMzQuMCA1ODQuMCA3NTAuMCA1NTYuMCA3NTAuMCAyMjIuMCA1NTYuMCAzMzMuMCAxMDAwLjAgNTU2LjAgNTU2LjAgMzMzLjAgMTAwMC4wIDY2Ny4wIDMzMy4wIDEwMDAuMCA3NTAuMCA2MTEuMCA3NTAuMCA3NTAuMCAyMjIuMCAyMjIuMCAzMzMuMCAzMzMuMCAzNTAuMCA1NTYuMCAxMDAwLjAgMzMzLjAgMTAwMC4wIDUwMC4wIDMzMy4wIDk0NC4wIDc1MC4wIDUwMC4wIDY2Ny4wIDI3OC4wIDMzMy4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDI2MC4wIDU1Ni4wIDMzMy4wIDczNy4wIDM3MC4wIDU1Ni4wIDU4NC4wIDMzMy4wIDczNy4wIDU1Mi4wIDQwMC4wIDU0OS4wIDMzMy4wIDMzMy4wIDMzMy4wIDU3Ni4wIDUzNy4wIDMzMy4wIDMzMy4wIDMzMy4wIDM2NS4wIDU1Ni4wIDgzNC4wIDgzNC4wIDgzNC4wIDYxMS4wIDY2Ny4wIDY2Ny4wIDY2Ny4wIDY2Ny4wIDY2Ny4wIDY2Ny4wIDEwMDAuMCA3MjIuMCA2NjcuMCA2NjcuMCA2NjcuMCA2NjcuMCAyNzguMCAyNzguMCAyNzguMCAyNzguMCA3MjIuMCA3MjIuMCA3NzguMCA3NzguMCA3NzguMCA3NzguMCA3NzguMCA1ODQuMCA3NzguMCA3MjIuMCA3MjIuMCA3MjIuMCA3MjIuMCA2NjcuMCA2NjcuMCA2MTEuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA4ODkuMCA1MDAuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCAyNzguMCAyNzguMCAyNzguMCAyNzguMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NDkuMCA2MTEuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1MDAuMCA1NTYuMCA1MDAuMCANCl0NCmVuZG9iag0KMTEgMCBvYmoNCjw8IC9UeXBlIC9FbmNvZGluZw0KL0Jhc2VFbmNvZGluZyAvV2luQW5zaUVuY29kaW5nDQo+Pg0KZW5kb2JqDQp4cmVmDQowIDE5DQowMDAwMDAwMDAwIDY1NTM1IGYNCjAwMDAwMDAwMTcgMDAwMDAgbg0KMDAwMDAwMDIwNSAwMDAwMCBuDQowMDAwMDAwMjYwIDAwMDAwIG4NCjAwMDAwMzk1NjkgMDAwMDAgbg0KMDAwMDAwMDMxMiAwMDAwMCBuDQowMDAwMDQ0MzExIDAwMDAwIG4NCjAwMDAwNDYwODEgMDAwMDAgbg0KMDAwMDA0NDQ5NSAwMDAwMCBuDQowMDAwMDQ0Njk0IDAwMDAwIG4NCjAwMDAwNDYxNTQgMDAwMDAgbg0KMDAwMDA0NzkxNiAwMDAwMCBuDQowMDAwMDQ2MzMyIDAwMDAwIG4NCjAwMDAwNDY1MjcgMDAwMDAgbg0KMDAwMDAzOTY5OSAwMDAwMCBuDQowMDAwMDIxMzI2IDAwMDAwIG4NCjAwMDAwMjE1ODkgMDAwMDAgbg0KMDAwMDAzOTMwNSAwMDAwMCBuDQowMDAwMDM5NjQ0IDAwMDAwIG4NCnRyYWlsZXINCjw8IC9TaXplIDE5DQovSW5mbyAxIDAgUg0KL1Jvb3QgMTggMCBSDQovSURbICA8MzA0MTMxMzEzOTMxNDUzOTJkMzA0MjM1MzQyZDM0MzAzMTQxMmQ0MjM4MzUzNTJkMzEzNTQ0NDMzNTQ2NDMzNzM0NDM0NDM2PiA8MzA0MTMxMzEzOTMxNDUzOTJkMzA0MjM1MzQyZDM0MzAzMTQxMmQ0MjM4MzUzNTJkMzEzNTQ0NDMzNTQ2NDMzNzM0NDM0NDM2PiBdDQo+Pg0Kc3RhcnR4cmVmDQo0Nzk5MA0KJSVFT0YNCg==', 'processing_log': False, 'error_message': False, 'monto_documento': 0, 'fecha_efectiva': False, 'compania_id': 1, 'proveedor_id': False, 'servicio_id': False, 'currency_id': 8, 'invoice_lines': []}) 
[heartbeat lun abr 27 08:43:12 -05 2026]
2026-04-27 13:43:13,895 1473585 INFO ? werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:13] "GET /web/static/src/img/spin.png HTTP/1.0" 304 - - - -
[heartbeat lun abr 27 08:43:14 -05 2026]
2026-04-27 13:43:16,230 1473585 DEBUG ? odoo.service.server: cron0 polling for jobs 
[heartbeat lun abr 27 08:43:16 -05 2026]
2026-04-27 13:43:17,181 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: process_xml_invoice(): archivo NO XML (fv09007947870212600011510.pdf). Se omite parseo XML. 
[heartbeat lun abr 27 08:43:18 -05 2026]
2026-04-27 13:43:18,876 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: Inicio action_procesar_documento para transcriptor.ocr 102 (company_id=1, proveedor_id=None) 
2026-04-27 13:43:20,307 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:20] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.008 50.065
2026-04-27 13:43:20,320 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:43:20 -05 2026]
[heartbeat lun abr 27 08:43:22 -05 2026]
2026-04-27 13:43:23,018 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: PDF convertido a 2 imágenes 
2026-04-27 13:43:23,601 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Enviando Petición 1 a OpenAI (Extracción de Texto y NIT) 
[heartbeat lun abr 27 08:43:24 -05 2026]
2026-04-27 13:43:24,756 1473585 DEBUG ? odoo.service.server: cron1 polling for jobs 
[heartbeat lun abr 27 08:43:26 -05 2026]
2026-04-27 13:43:27,463 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:27] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.010 50.152
2026-04-27 13:43:27,475 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:43:28 -05 2026]
[heartbeat lun abr 27 08:43:30 -05 2026]
[heartbeat lun abr 27 08:43:32 -05 2026]
[heartbeat lun abr 27 08:43:34 -05 2026]
[heartbeat lun abr 27 08:43:36 -05 2026]
[heartbeat lun abr 27 08:43:38 -05 2026]
[heartbeat lun abr 27 08:43:40 -05 2026]
[heartbeat lun abr 27 08:43:42 -05 2026]
[heartbeat lun abr 27 08:43:44 -05 2026]
[heartbeat lun abr 27 08:43:46 -05 2026]
[heartbeat lun abr 27 08:43:48 -05 2026]
[heartbeat lun abr 27 08:43:50 -05 2026]
2026-04-27 13:43:16,230 1473585 DEBUG ? odoo.service.server: cron0 polling for jobs 
2026-04-27 13:43:17,181 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: process_xml_invoice(): archivo NO XML (fv09007947870212600011510.pdf). Se omite parseo XML. 
2026-04-27 13:43:18,876 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: Inicio action_procesar_documento para transcriptor.ocr 102 (company_id=1, proveedor_id=None) 
2026-04-27 13:43:20,307 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:20] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.008 50.065
2026-04-27 13:43:20,320 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
2026-04-27 13:43:23,018 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: PDF convertido a 2 imágenes 
2026-04-27 13:43:23,601 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Enviando Petición 1 a OpenAI (Extracción de Texto y NIT) 
2026-04-27 13:43:24,756 1473585 DEBUG ? odoo.service.server: cron1 polling for jobs 
2026-04-27 13:43:27,463 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:27] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.010 50.152
2026-04-27 13:43:27,475 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:44:03 -05 2026]
[heartbeat lun abr 27 08:44:05 -05 2026]
[heartbeat lun abr 27 08:44:07 -05 2026]
[heartbeat lun abr 27 08:44:09 -05 2026]
2026-04-27 13:44:10,612 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:44:10] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.007 50.287
2026-04-27 13:44:10,649 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:44:11 -05 2026]
[heartbeat lun abr 27 08:44:13 -05 2026]
[heartbeat lun abr 27 08:44:15 -05 2026]
2026-04-27 13:44:16,277 1473585 DEBUG ? odoo.service.server: cron0 polling for jobs 
2026-04-27 13:44:16,776 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Respuesta de Petición 1 recibida correctamente 
2026-04-27 13:44:16,777 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: NIT detectado por OpenAI: 900794787 
2026-04-27 13:44:16,910 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: Proveedor detectado por NIT: FASTER SERVICES COLOMBIA S.A.S. 
2026-04-27 13:44:16,910 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: Iniciando Petición 2 (Generación JSON) para transcriptor.ocr 102 
2026-04-27 13:44:16,910 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Enviando Petición 2 a OpenAI (Extracción JSON Final) 
2026-04-27 13:44:17,719 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:44:17] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.011 50.235
2026-04-27 13:44:17,729 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:44:17 -05 2026]
[heartbeat lun abr 27 08:44:19 -05 2026]
[heartbeat lun abr 27 08:44:21 -05 2026]
2026-04-27 13:44:23,056 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Respuesta de Petición 2 recibida correctamente 
2026-04-27 13:44:23,148 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR terminado para extractor 168 en 65.16s 
2026-04-27 13:44:23,150 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Datos extraídos para extractor 168: claves JSON=['nit_proveedor', 'nombre_proveedor', 'numero_factura', 'nit_cliente', 'fecha_emision', 'total_a_pagar', 'line_items'], datos_mapeados=['fecha_efectiva', 'invoice_number', 'nit_proveedor', 'nombre_proveedor'] 
2026-04-27 13:44:23,207 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Proveedor copiado desde transcriptor.ocr 102: FASTER SERVICES COLOMBIA S.A.S. 
2026-04-27 13:44:23,568 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'ALMACENAMIENTO CAJA' asignada al servicio 'SERVICIO DE ALMACENAMIENTO Y LOGISTICA TURBACO' (Ratio: 100.00%) 
2026-04-27 13:44:23,738 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'ESTAMPILLADO UNIDAD' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,767 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'ALISTAMIENTO CAJA' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,799 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'IMPRESION DE FACTURAS' asignada al servicio 'OTROS SERVICIOS' (Ratio: 95.24%) 
[heartbeat lun abr 27 08:44:23 -05 2026]
2026-04-27 13:44:23,830 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'CARGUE Y DESCARGUE CAJA' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,860 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'DESCARGUE OPERATIVO' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,890 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'ARMADO DE OFERTAS' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,914 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'DESTRUCCION' asignada al servicio 'OTROS SERVICIOS' (Ratio: 90.91%) 
2026-04-27 13:44:23,918 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Auto-propagación: servicio_id SERVICIO DE ALMACENAMIENTO Y LOGISTICA TURBACO asignado a la cabecera desde la línea. 
2026-04-27 13:44:23,919 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Automatización OCR: Documento 168 tiene proveedor y servicio. Intentando validar y crear factura... 
2026-04-27 13:44:24,191 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta Analítica encontrada para ciudad 'santa marta': SANTA MARTA                              (ID: 662) 
2026-04-27 13:44:24,442 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta analítica SANTA MARTA                              asignada a línea con cuenta contable 52359502 
2026-04-27 13:44:24,444 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta analítica SANTA MARTA                              asignada a línea con cuenta contable 52359501 
2026-04-27 13:44:24,445 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta analítica SANTA MARTA                              asignada a línea con cuenta contable 52359501 
2026-04-27 13:44:24,445 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta analítica SANTA MARTA                              asignada a línea con cuenta contable 52359501 eug: 127.0.0.1 - - [27/Apr/2026 13:42:59] "GET /web_enterprise/static/src/img/down-arrow.png HTTP/1.0" 304 - - - -
2026-04-27 13:43:00,820 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:00] "POST /mail/init_messaging HTTP/1.0" 200 - 144 840.688 0.754
[heartbeat lun abr 27 08:43:04 -05 2026]
[heartbeat lun abr 27 08:43:06 -05 2026]
[heartbeat lun abr 27 08:43:08 -05 2026]
[heartbeat lun abr 27 08:43:10 -05 2026]
2026-04-27 13:43:10,888 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
2026-04-27 13:43:10,892 1473585 DEBUG dismel odoo.api: call dian.invoice.extractor().create({'state': 'draft', 'payable_amount': 0, 'invoice_number': False, 'supplier_name': False, 'customer_name': False, 'cufe': False, 'invoice_type_code': False, 'invoice_uuid': False, 'issue_date': False, 'issue_time': False, 'due_date': False, 'currency_code': 'COP', 'invoice_period_start': False, 'invoice_period_end': False, 'upload_date': '2026-04-27 13:42:59', 'supplier_nit': False, 'supplier_company_id': False, 'supplier_tax_level': False, 'supplier_address': False, 'supplier_city': False, 'supplier_department': False, 'supplier_phone': False, 'supplier_email': False, 'customer_nit': False, 'customer_company_id': False, 'customer_tax_level': False, 'customer_address': False, 'customer_city': False, 'customer_department': False, 'customer_phone': False, 'customer_email': False, 'dian_authorization': False, 'dian_authorization_start': False, 'dian_authorization_end': False, 'dian_prefix': False, 'dian_from': False, 'dian_to': False, 'software_provider_id': False, 'software_id': False, 'dian_response_code': False, 'dian_validation_date': False, 'dian_validation_time': False, 'dian_response_description': False, 'qr_code': False, 'line_extension_amount': 0, 'tax_exclusive_amount': 0, 'tax_inclusive_amount': 0, 'allowance_total_amount': 0, 'charge_total_amount': 0, 'prepaid_amount': 0, 'total_tax_amount': 0, 'total_iva': 0, 'total_rete_fuente': 0, 'total_rete_iva': 0, 'total_rete_ica': 0, 'payment_means_code': False, 'payment_id': False, 'payment_due_date': False, 'purchase_order': False, 'dispatch_document': False, 'receipt_document': False, 'additional_document_ref': False, 'file_name': 'fv09007947870212600011510.pdf', 'file_data': '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Nzc4LjAgNjY3LjAgNzc4LjAgNzIyLjAgNjY3LjAgNjExLjAgNzIyLjAgNjY3LjAgOTQ0LjAgNjY3LjAgNjY3LjAgNjExLjAgMzMzLjAgMjc4LjAgMzMzLjAgNTg0LjAgNTU2LjAgMzMzLjAgNTU2LjAgNjExLjAgNTU2LjAgNjExLjAgNTU2LjAgMzMzLjAgNjExLjAgNjExLjAgMjc4LjAgMjc4LjAgNTU2LjAgMjc4LjAgODg5LjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgMzg5LjAgNTU2LjAgMzMzLjAgNjExLjAgNTU2LjAgNzc4LjAgNTU2LjAgNTU2LjAgNTAwLjAgMzg5LjAgMjgwLjAgMzg5LjAgNTg0LjAgNzUwLjAgNTU2LjAgNzUwLjAgMjc4LjAgNTU2LjAgNTAwLjAgMTAwMC4wIDU1Ni4wIDU1Ni4wIDMzMy4wIDEwMDAuMCA2NjcuMCAzMzMuMCAxMDAwLjAgNzUwLjAgNjExLjAgNzUwLjAgNzUwLjAgMjc4LjAgMjc4LjAgNTAwLjAgNTAwLjAgMzUwLjAgNTU2LjAgMTAwMC4wIDMzMy4wIDEwMDAuMCA1NTYuMCAzMzMuMCA5NDQuMCA3NTAuMCA1MDAuMCA2NjcuMCAyNzguMCAzMzMuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCAyODAuMCA1NTYuMCAzMzMuMCA3MzcuMCAzNzAuMCA1NTYuMCA1ODQuMCAzMzMuMCA3MzcuMCA1NTIuMCA0MDAuMCA1NDkuMCAzMzMuMCAzMzMuMCAzMzMuMCA1NzYuMCA1NTYuMCAzMzMuMCAzMzMuMCAzMzMuMCAzNjUuMCA1NTYuMCA4MzQuMCA4MzQuMCA4MzQuMCA2MTEuMCA3MjIuMCA3MjIuMCA3MjIuMCA3MjIuMCA3MjIuMCA3MjIuMCAxMDAwLjAgNzIyLjAgNjY3LjAgNjY3LjAgNjY3LjAgNjY3LjAgMjc4LjAgMjc4LjAgMjc4LjAgMjc4LjAgNzIyLjAgNzIyLjAgNzc4LjAgNzc4LjAgNzc4LjAgNzc4LjAgNzc4LjAgNTg0LjAgNzc4LjAgNzIyLjAgNzIyLjAgNzIyLjAgNzIyLjAgNjY3LjAgNjY3LjAgNjExLjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgODg5LjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgNTU2LjAgMjc4LjAgMjc4LjAgMjc4LjAgMjc4LjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNTQ5LjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNjExLjAgNTU2LjAgNjExLjAgNTU2LjAgDQpdDQplbmRvYmoNCjcgMCBvYmoNCjw8IC9UeXBlIC9FbmNvZGluZw0KL0Jhc2VFbmNvZGluZyAvV2luQW5zaUVuY29kaW5nDQo+Pg0KZW5kb2JqDQoxMCAwIG9iag0KPDwgL1R5cGUgL0ZvbnQNCi9TdWJ0eXBlIC9UcnVlVHlwZQ0KL05hbWUgL0FyaWFsDQovQmFzZUZvbnQgL0FyaWFsDQovRmlyc3RDaGFyIDMwDQovTGFzdENoYXIgMjU1DQovRm9udERlc2NyaXB0b3IgMTIgMCBSDQovV2lkdGhzIDEzIDAgUg0KL0VuY29kaW5nIDExIDAgUg0KPj4NCmVuZG9iag0KMTIgMCBvYmoNCjw8IC9UeXBlIC9Gb250RGVzY3JpcHRvcg0KL0ZvbnROYW1lIC9BcmlhbA0KL0FzY2VudCA5MDUuMA0KL0NhcEhlaWdodCA5MDUuMA0KL0Rlc2NlbnQgLTIxMi4wDQovRmxhZ3MgMzINCi9Gb250QkJveCBbIC0yNTAuMCAtMjEyLjAgMjY2NS4wIDkwNS4wIF0NCi9JdGFsaWNBbmdsZSAwDQovU3RlbVYgMA0KPj4NCmVuZG9iag0KMTMgMCBvYmoNClsNCjc1MC4wIDc1MC4wIDI3OC4wIDI3OC4wIDM1NS4wIDU1Ni4wIDU1Ni4wIDg4OS4wIDY2Ny4wIDE5MS4wIDMzMy4wIDMzMy4wIDM4OS4wIDU4NC4wIDI3OC4wIDMzMy4wIDI3OC4wIDI3OC4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDI3OC4wIDI3OC4wIDU4NC4wIDU4NC4wIDU4NC4wIDU1Ni4wIDEwMTUuMCA2NjcuMCA2NjcuMCA3MjIuMCA3MjIuMCA2NjcuMCA2MTEuMCA3NzguMCA3MjIuMCAyNzguMCA1MDAuMCA2NjcuMCA1NTYuMCA4MzMuMCA3MjIuMCA3NzguMCA2NjcuMCA3NzguMCA3MjIuMCA2NjcuMCA2MTEuMCA3MjIuMCA2NjcuMCA5NDQuMCA2NjcuMCA2NjcuMCA2MTEuMCAyNzguMCAyNzguMCAyNzguMCA0NjkuMCA1NTYuMCAzMzMuMCA1NTYuMCA1NTYuMCA1MDAuMCA1NTYuMCA1NTYuMCAyNzguMCA1NTYuMCA1NTYuMCAyMjIuMCAyMjIuMCA1MDAuMCAyMjIuMCA4MzMuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCAzMzMuMCA1MDAuMCAyNzguMCA1NTYuMCA1MDAuMCA3MjIuMCA1MDAuMCA1MDAuMCA1MDAuMCAzMzQuMCAyNjAuMCAzMzQuMCA1ODQuMCA3NTAuMCA1NTYuMCA3NTAuMCAyMjIuMCA1NTYuMCAzMzMuMCAxMDAwLjAgNTU2LjAgNTU2LjAgMzMzLjAgMTAwMC4wIDY2Ny4wIDMzMy4wIDEwMDAuMCA3NTAuMCA2MTEuMCA3NTAuMCA3NTAuMCAyMjIuMCAyMjIuMCAzMzMuMCAzMzMuMCAzNTAuMCA1NTYuMCAxMDAwLjAgMzMzLjAgMTAwMC4wIDUwMC4wIDMzMy4wIDk0NC4wIDc1MC4wIDUwMC4wIDY2Ny4wIDI3OC4wIDMzMy4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDU1Ni4wIDI2MC4wIDU1Ni4wIDMzMy4wIDczNy4wIDM3MC4wIDU1Ni4wIDU4NC4wIDMzMy4wIDczNy4wIDU1Mi4wIDQwMC4wIDU0OS4wIDMzMy4wIDMzMy4wIDMzMy4wIDU3Ni4wIDUzNy4wIDMzMy4wIDMzMy4wIDMzMy4wIDM2NS4wIDU1Ni4wIDgzNC4wIDgzNC4wIDgzNC4wIDYxMS4wIDY2Ny4wIDY2Ny4wIDY2Ny4wIDY2Ny4wIDY2Ny4wIDY2Ny4wIDEwMDAuMCA3MjIuMCA2NjcuMCA2NjcuMCA2NjcuMCA2NjcuMCAyNzguMCAyNzguMCAyNzguMCAyNzguMCA3MjIuMCA3MjIuMCA3NzguMCA3NzguMCA3NzguMCA3NzguMCA3NzguMCA1ODQuMCA3NzguMCA3MjIuMCA3MjIuMCA3MjIuMCA3MjIuMCA2NjcuMCA2NjcuMCA2MTEuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA4ODkuMCA1MDAuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCAyNzguMCAyNzguMCAyNzguMCAyNzguMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NDkuMCA2MTEuMCA1NTYuMCA1NTYuMCA1NTYuMCA1NTYuMCA1MDAuMCA1NTYuMCA1MDAuMCANCl0NCmVuZG9iag0KMTEgMCBvYmoNCjw8IC9UeXBlIC9FbmNvZGluZw0KL0Jhc2VFbmNvZGluZyAvV2luQW5zaUVuY29kaW5nDQo+Pg0KZW5kb2JqDQp4cmVmDQowIDE5DQowMDAwMDAwMDAwIDY1NTM1IGYNCjAwMDAwMDAwMTcgMDAwMDAgbg0KMDAwMDAwMDIwNSAwMDAwMCBuDQowMDAwMDAwMjYwIDAwMDAwIG4NCjAwMDAwMzk1NjkgMDAwMDAgbg0KMDAwMDAwMDMxMiAwMDAwMCBuDQowMDAwMDQ0MzExIDAwMDAwIG4NCjAwMDAwNDYwODEgMDAwMDAgbg0KMDAwMDA0NDQ5NSAwMDAwMCBuDQowMDAwMDQ0Njk0IDAwMDAwIG4NCjAwMDAwNDYxNTQgMDAwMDAgbg0KMDAwMDA0NzkxNiAwMDAwMCBuDQowMDAwMDQ2MzMyIDAwMDAwIG4NCjAwMDAwNDY1MjcgMDAwMDAgbg0KMDAwMDAzOTY5OSAwMDAwMCBuDQowMDAwMDIxMzI2IDAwMDAwIG4NCjAwMDAwMjE1ODkgMDAwMDAgbg0KMDAwMDAzOTMwNSAwMDAwMCBuDQowMDAwMDM5NjQ0IDAwMDAwIG4NCnRyYWlsZXINCjw8IC9TaXplIDE5DQovSW5mbyAxIDAgUg0KL1Jvb3QgMTggMCBSDQovSURbICA8MzA0MTMxMzEzOTMxNDUzOTJkMzA0MjM1MzQyZDM0MzAzMTQxMmQ0MjM4MzUzNTJkMzEzNTQ0NDMzNTQ2NDMzNzM0NDM0NDM2PiA8MzA0MTMxMzEzOTMxNDUzOTJkMzA0MjM1MzQyZDM0MzAzMTQxMmQ0MjM4MzUzNTJkMzEzNTQ0NDMzNTQ2NDMzNzM0NDM0NDM2PiBdDQo+Pg0Kc3RhcnR4cmVmDQo0Nzk5MA0KJSVFT0YNCg==', 'processing_log': False, 'error_message': False, 'monto_documento': 0, 'fecha_efectiva': False, 'compania_id': 1, 'proveedor_id': False, 'servicio_id': False, 'currency_id': 8, 'invoice_lines': []}) 
[heartbeat lun abr 27 08:43:12 -05 2026]
2026-04-27 13:43:13,895 1473585 INFO ? werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:13] "GET /web/static/src/img/spin.png HTTP/1.0" 304 - - - -
[heartbeat lun abr 27 08:43:14 -05 2026]
2026-04-27 13:43:16,230 1473585 DEBUG ? odoo.service.server: cron0 polling for jobs 
[heartbeat lun abr 27 08:43:16 -05 2026]
2026-04-27 13:43:17,181 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: process_xml_invoice(): archivo NO XML (fv09007947870212600011510.pdf). Se omite parseo XML. 
[heartbeat lun abr 27 08:43:18 -05 2026]
2026-04-27 13:43:18,876 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: Inicio action_procesar_documento para transcriptor.ocr 102 (company_id=1, proveedor_id=None) 
2026-04-27 13:43:20,307 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:20] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.008 50.065
2026-04-27 13:43:20,320 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:43:20 -05 2026]
[heartbeat lun abr 27 08:43:22 -05 2026]
2026-04-27 13:43:23,018 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: PDF convertido a 2 imágenes 
2026-04-27 13:43:23,601 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Enviando Petición 1 a OpenAI (Extracción de Texto y NIT) 
[heartbeat lun abr 27 08:43:24 -05 2026]
2026-04-27 13:43:24,756 1473585 DEBUG ? odoo.service.server: cron1 polling for jobs 
[heartbeat lun abr 27 08:43:26 -05 2026]
2026-04-27 13:43:27,463 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:27] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.010 50.152
2026-04-27 13:43:27,475 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:43:28 -05 2026]
[heartbeat lun abr 27 08:43:30 -05 2026]
[heartbeat lun abr 27 08:43:32 -05 2026]
[heartbeat lun abr 27 08:43:34 -05 2026]
[heartbeat lun abr 27 08:43:36 -05 2026]
[heartbeat lun abr 27 08:43:38 -05 2026]
[heartbeat lun abr 27 08:43:40 -05 2026]
[heartbeat lun abr 27 08:43:42 -05 2026]
[heartbeat lun abr 27 08:43:44 -05 2026]
[heartbeat lun abr 27 08:43:46 -05 2026]
[heartbeat lun abr 27 08:43:48 -05 2026]
[heartbeat lun abr 27 08:43:50 -05 2026]
2026-04-27 13:43:16,230 1473585 DEBUG ? odoo.service.server: cron0 polling for jobs 
2026-04-27 13:43:17,181 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: process_xml_invoice(): archivo NO XML (fv09007947870212600011510.pdf). Se omite parseo XML. 
2026-04-27 13:43:18,876 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: Inicio action_procesar_documento para transcriptor.ocr 102 (company_id=1, proveedor_id=None) 
2026-04-27 13:43:20,307 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:20] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.008 50.065
2026-04-27 13:43:20,320 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
2026-04-27 13:43:23,018 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: PDF convertido a 2 imágenes 
2026-04-27 13:43:23,601 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Enviando Petición 1 a OpenAI (Extracción de Texto y NIT) 
2026-04-27 13:43:24,756 1473585 DEBUG ? odoo.service.server: cron1 polling for jobs 
2026-04-27 13:43:27,463 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:43:27] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.010 50.152
2026-04-27 13:43:27,475 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:44:03 -05 2026]
[heartbeat lun abr 27 08:44:05 -05 2026]
[heartbeat lun abr 27 08:44:07 -05 2026]
[heartbeat lun abr 27 08:44:09 -05 2026]
2026-04-27 13:44:10,612 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:44:10] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.007 50.287
2026-04-27 13:44:10,649 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:44:11 -05 2026]
[heartbeat lun abr 27 08:44:13 -05 2026]
[heartbeat lun abr 27 08:44:15 -05 2026]
2026-04-27 13:44:16,277 1473585 DEBUG ? odoo.service.server: cron0 polling for jobs 
2026-04-27 13:44:16,776 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Respuesta de Petición 1 recibida correctamente 
2026-04-27 13:44:16,777 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: NIT detectado por OpenAI: 900794787 
2026-04-27 13:44:16,910 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: Proveedor detectado por NIT: FASTER SERVICES COLOMBIA S.A.S. 
2026-04-27 13:44:16,910 1473585 INFO dismel odoo.addons.transcriptor_ocr.models.ocr_document: Iniciando Petición 2 (Generación JSON) para transcriptor.ocr 102 
2026-04-27 13:44:16,910 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Enviando Petición 2 a OpenAI (Extracción JSON Final) 
2026-04-27 13:44:17,719 1473585 INFO dismel werkzeug: 127.0.0.1 - - [27/Apr/2026 13:44:17] "POST /longpolling/poll HTTP/1.0" 200 - 8 0.011 50.235
2026-04-27 13:44:17,729 1473585 DEBUG dismel odoo.modules.registry: Multiprocess signaling check: [Registry - 27334 -> 27334] [Cache - 110582 -> 110582] 
[heartbeat lun abr 27 08:44:17 -05 2026]
[heartbeat lun abr 27 08:44:19 -05 2026]
[heartbeat lun abr 27 08:44:21 -05 2026]
2026-04-27 13:44:23,056 1473585 INFO dismel odoo.addons.transcriptor_ocr.services.llm_ocr_service: Respuesta de Petición 2 recibida correctamente 
2026-04-27 13:44:23,148 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR terminado para extractor 168 en 65.16s 
2026-04-27 13:44:23,150 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Datos extraídos para extractor 168: claves JSON=['nit_proveedor', 'nombre_proveedor', 'numero_factura', 'nit_cliente', 'fecha_emision', 'total_a_pagar', 'line_items'], datos_mapeados=['fecha_efectiva', 'invoice_number', 'nit_proveedor', 'nombre_proveedor'] 
2026-04-27 13:44:23,207 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Proveedor copiado desde transcriptor.ocr 102: FASTER SERVICES COLOMBIA S.A.S. 
2026-04-27 13:44:23,568 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'ALMACENAMIENTO CAJA' asignada al servicio 'SERVICIO DE ALMACENAMIENTO Y LOGISTICA TURBACO' (Ratio: 100.00%) 
2026-04-27 13:44:23,738 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'ESTAMPILLADO UNIDAD' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,767 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'ALISTAMIENTO CAJA' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,799 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'IMPRESION DE FACTURAS' asignada al servicio 'OTROS SERVICIOS' (Ratio: 95.24%) 
[heartbeat lun abr 27 08:44:23 -05 2026]
2026-04-27 13:44:23,830 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'CARGUE Y DESCARGUE CAJA' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,860 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'DESCARGUE OPERATIVO' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,890 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'ARMADO DE OFERTAS' asignada al servicio 'OTROS SERVICIOS' (Ratio: 100.00%) 
2026-04-27 13:44:23,914 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: OCR Fuzzy Match: Línea 'DESTRUCCION' asignada al servicio 'OTROS SERVICIOS' (Ratio: 90.91%) 
2026-04-27 13:44:23,918 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Auto-propagación: servicio_id SERVICIO DE ALMACENAMIENTO Y LOGISTICA TURBACO asignado a la cabecera desde la línea. 
2026-04-27 13:44:23,919 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Automatización OCR: Documento 168 tiene proveedor y servicio. Intentando validar y crear factura... 
2026-04-27 13:44:24,191 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta Analítica encontrada para ciudad 'santa marta': SANTA MARTA                              (ID: 662) 
2026-04-27 13:44:24,442 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta analítica SANTA MARTA                              asignada a línea con cuenta contable 52359502 
2026-04-27 13:44:24,444 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta analítica SANTA MARTA                              asignada a línea con cuenta contable 52359501 
2026-04-27 13:44:24,445 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta analítica SANTA MARTA                              asignada a línea con cuenta contable 52359501 
2026-04-27 13:44:24,445 1473585 INFO dismel odoo.addons.causacion_terceros_autorizaciones.models.dian_invoice_extractor: Cuenta analítica SANTA MARTA                              asignada a línea con cuenta contable 52359501 



