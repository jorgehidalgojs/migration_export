import json
import logging
from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Catálogo completo de modelos exportables y sus campos
# ---------------------------------------------------------------------------
EXPORTABLE_MODELS = {
    # --- Base / Unidades ---
    'uom.category':             ['name'],
    'uom.uom':                  ['name', 'category_id', 'uom_type', 'factor', 'rounding', 'active'],
    # --- Localización ---
    'res.lang':                 ['name', 'code', 'iso_code', 'active', 'date_format', 'time_format',
                                 'grouping', 'decimal_point', 'thousands_sep'],
    'res.country':              ['name', 'code', 'phone_code', 'currency_id'],
    'res.country.state':        ['name', 'code', 'country_id'],
    'res.currency':             ['name', 'symbol', 'active', 'rounding', 'decimal_places'],
    'res.bank':                 ['name', 'bic', 'country_id', 'street', 'city', 'phone', 'email'],
    # --- Contactos: metadatos ---
    'res.partner.title':        ['name', 'shortcut'],
    'res.partner.industry':     ['name', 'full_name'],
    'res.partner.category':     ['name', 'parent_id', 'color'],
    # --- Productos ---
    'product.category':         ['name', 'parent_id', 'property_cost_method'],
    'product.tag':              ['name', 'color'],
    'product.template':         ['name', 'type', 'categ_id', 'uom_id', 'uom_po_id',
                                 'sale_ok', 'purchase_ok', 'active', 'description',
                                 'description_sale', 'description_purchase',
                                 'list_price', 'standard_price', 'default_code', 'barcode',
                                 'weight', 'volume', 'tracking'],
    # --- Contabilidad analítica ---
    'account.analytic.group':   ['name', 'parent_id', 'description'],
    'account.analytic.account': ['name', 'code', 'group_id', 'active', 'company_id', 'partner_id'],
    # --- Contabilidad ---
    'account.account':          ['name', 'code', 'deprecated', 'reconcile', 'company_id', 'note'],
    'account.journal':          ['name', 'code', 'type', 'company_id', 'currency_id',
                                 'default_account_id'],
    'account.tax':              ['name', 'type_tax_use', 'amount', 'amount_type',
                                 'description', 'active', 'company_id', 'price_include',
                                 'include_base_amount'],
    'account.payment.term':     ['name', 'note', 'company_id'],
    'account.incoterms':        ['name', 'code'],
    'account.fiscal.position':  ['name', 'company_id', 'auto_apply', 'vat_required',
                                 'note', 'country_id', 'country_group_id'],
    'account.fiscal.position.tax': ['position_id', 'tax_src_id', 'tax_dest_id'],
    # --- RRHH: estructura ---
    'resource.calendar':        ['name', 'company_id', 'tz', 'two_weeks_calendar'],
    'hr.department':            ['name', 'parent_id', 'company_id'],
    'hr.job':                   ['name', 'department_id', 'no_of_recruitment', 'description'],
    'hr.work.entry.type':       ['name', 'code', 'color', 'leave_type_id'],
    'hr.leave.type':            ['name', 'allocation_type', 'leave_validation_type',
                                 'company_id', 'request_unit', 'time_type', 'color'],
    'hr.contract.type':         ['name'],
    # --- RRHH: nómina (opcional — solo si el módulo está instalado) ---
    'hr.payroll.structure.type': ['name', 'wage_type', 'default_resource_calendar_id'],
    'hr.payroll.structure':     ['name', 'type_id', 'company_id'],
    # --- CRM ---
    'crm.team':                 ['name', 'company_id', 'user_id', 'active', 'sequence'],
    'crm.stage':                ['name', 'sequence', 'probability', 'fold'],
    'crm.tag':                  ['name', 'color'],
    # --- Almacén / Stock ---
    'stock.warehouse':          ['name', 'code', 'company_id', 'active'],
    'stock.location':           ['name', 'complete_name', 'usage', 'location_id',
                                 'company_id', 'active', 'barcode'],
    'stock.picking.type':       ['name', 'code', 'warehouse_id', 'sequence_code',
                                 'company_id', 'active'],
    'stock.route':              ['name', 'active', 'company_id'],
    # --- Usuarios internos ---
    'res.users':                ['name', 'login', 'email', 'active', 'lang', 'tz',
                                 'signature', 'company_id'],
    # --- Contactos ---
    'res.partner':              ['name', 'email', 'phone', 'mobile', 'street', 'street2',
                                 'city', 'zip', 'country_id', 'state_id', 'vat', 'is_company',
                                 'active', 'customer_rank', 'supplier_rank', 'company_type',
                                 'company_id', 'lang', 'website', 'comment', 'title',
                                 'industry_id', 'category_id', 'parent_id', 'type',
                                 'function', 'ref', 'property_payment_term_id',
                                 'property_supplier_payment_term_id'],
    'res.partner.bank':         ['acc_number', 'partner_id', 'bank_id', 'currency_id',
                                 'company_id', 'sequence'],
    # --- Precios ---
    'product.pricelist':        ['name', 'currency_id', 'active', 'company_id'],
    'product.pricelist.item':   ['pricelist_id', 'compute_price', 'applied_on',
                                 'product_tmpl_id', 'product_id', 'categ_id',
                                 'min_quantity', 'fixed_price', 'percent_price',
                                 'price_discount', 'price_surcharge', 'date_start', 'date_end'],
    'product.supplierinfo':     ['product_tmpl_id', 'name', 'product_name',
                                 'product_code', 'min_qty', 'price', 'currency_id',
                                 'delay', 'company_id'],
    # --- Empleados ---
    'hr.employee':              ['name', 'department_id', 'job_id', 'job_title', 'company_id',
                                 'resource_calendar_id', 'work_email', 'work_phone',
                                 'mobile_phone', 'active', 'parent_id', 'coach_id',
                                 'address_id', 'address_home_id', 'gender', 'marital',
                                 'country_id', 'identification_id', 'passport_id',
                                 'certificate', 'study_field', 'study_school',
                                 'emergency_contact', 'emergency_phone'],
    # --- Proyecto ---
    'project.tags':             ['name', 'color'],
    'project.project':          ['name', 'partner_id', 'user_id', 'date', 'date_start',
                                 'description', 'active', 'company_id', 'tag_ids'],
    'project.task.type':        ['name', 'fold', 'sequence'],
    # --- Secuencias ---
    'ir.sequence':              ['name', 'code', 'prefix', 'suffix', 'padding',
                                 'number_next', 'number_increment', 'implementation',
                                 'active', 'company_id'],
    # ===================================================================
    # ===================================================================
    # Procesos abiertos
    # ===================================================================
    'sale.order':               ['name', 'partner_id', 'state', 'date_order', 'validity_date',
                                 'commitment_date', 'company_id', 'currency_id', 'note',
                                 'payment_term_id', 'user_id', 'team_id', 'pricelist_id',
                                 'fiscal_position_id', 'incoterm'],
    'purchase.order':           ['name', 'partner_id', 'state', 'date_order', 'date_planned',
                                 'company_id', 'currency_id', 'notes', 'payment_term_id',
                                 'user_id', 'incoterm_id', 'fiscal_position_id'],
    'account.move':             ['name', 'partner_id', 'move_type', 'state', 'invoice_date',
                                 'invoice_date_due', 'amount_untaxed', 'amount_tax',
                                 'amount_total', 'company_id', 'currency_id', 'ref',
                                 'narration', 'payment_reference', 'fiscal_position_id',
                                 'invoice_payment_term_id'],
    'project.task':             ['name', 'project_id', 'user_ids', 'stage_id', 'priority',
                                 'date_deadline', 'description', 'kanban_state', 'company_id',
                                 'partner_id', 'tag_ids'],
    'hr.leave':                 ['employee_id', 'holiday_status_id', 'state',
                                 'date_from', 'date_to', 'number_of_days',
                                 'private_name'],
}

# Dominios para filtrar sólo registros "abiertos" en procesos
OPEN_PROCESS_DOMAINS = {
    # Solo usuarios internos (excluye portal/público)
    'res.users':     [('share', '=', False)],
    'sale.order':    [('state', 'in', ['draft', 'sent', 'sale'])],
    'purchase.order': [('state', 'in', ['draft', 'sent', 'purchase'])],
    'account.move':  [('state', '=', 'draft')],
    'project.task':  [('stage_id.fold', '=', False)],
    'hr.leave':      [('state', 'not in', ['refuse', 'validate'])],
    # Stock: solo ubicaciones internas y de tránsito (excluir vistas/virtuales)
    'stock.location': [('usage', 'in', ['internal', 'transit', 'customer', 'supplier'])],
    # Secuencias: solo las activas y no de sistema
    'ir.sequence':   [('active', '=', True), ('code', '!=', False)],
}

MAX_BATCH_SIZE = 500


class MigrationExportController(http.Controller):

    def _check_api_key(self):
        api_key = request.httprequest.headers.get('X-Migration-Key')
        if not api_key:
            return False
        valid_key = request.env['ir.config_parameter'].sudo().get_param(
            'migration_export.api_key'
        )
        return bool(api_key and valid_key and api_key == valid_key)

    def _json_response(self, data, status=200):
        return Response(
            json.dumps(data, default=str),
            status=status,
            mimetype='application/json',
        )

    @http.route('/migration/export/ping', type='http', auth='none',
                methods=['GET'], csrf=False)
    def ping(self, **kw):
        return self._json_response({'status': 'ok', 'version': '15'})

    @http.route('/migration/export/models', type='http', auth='none',
                methods=['GET'], csrf=False)
    def list_models(self, **kw):
        if not self._check_api_key():
            return self._json_response({'error': 'Unauthorized'}, 401)

        result = {}
        for model_name in EXPORTABLE_MODELS:
            try:
                domain = OPEN_PROCESS_DOMAINS.get(model_name, [])
                count = request.env[model_name].sudo().search_count(domain)
                result[model_name] = {'count': count}
            except Exception as exc:
                result[model_name] = {'error': str(exc)}

        return self._json_response({'models': result})

    @http.route('/migration/export/batch', type='http', auth='none',
                methods=['GET'], csrf=False)
    def export_batch(self, model=None, offset=0, limit=100, company_id=None, **kw):
        """
        GET /migration/export/batch?model=res.partner&offset=0&limit=100&company_id=1
        """
        if not self._check_api_key():
            return self._json_response({'error': 'Unauthorized'}, 401)

        if not model or model not in EXPORTABLE_MODELS:
            return self._json_response(
                {'error': f'Modelo "{model}" no exportable'}, 400
            )

        try:
            offset = int(offset)
            limit = min(int(limit), MAX_BATCH_SIZE)
            fields_to_read = EXPORTABLE_MODELS[model]
            domain = list(OPEN_PROCESS_DOMAINS.get(model, []))

            # Filtrar por empresa si el modelo lo soporta y se pasó company_id
            if company_id:
                Model = request.env[model]
                if 'company_id' in Model._fields:
                    domain.append(('company_id', '=', int(company_id)))

            records = request.env[model].sudo().search_read(
                domain, fields_to_read,
                offset=offset, limit=limit, order='id asc',
            )
            total = request.env[model].sudo().search_count(domain)

            return self._json_response({
                'model':    model,
                'offset':   offset,
                'limit':    limit,
                'total':    total,
                'count':    len(records),
                'has_more': (offset + limit) < total,
                'records':  records,
            })

        except Exception as exc:
            _logger.exception("Export error [%s]: %s", model, exc)
            return self._json_response({'error': str(exc)}, 500)

    @http.route('/migration/export/introspect', type='http', auth='none',
                methods=['GET'], csrf=False)
    def introspect_model(self, model=None, **kw):
        if not self._check_api_key():
            return self._json_response({'error': 'Unauthorized'}, 401)
        if not model:
            return self._json_response({'error': 'Parámetro model requerido'}, 400)
        try:
            ir_model = request.env['ir.model'].sudo().search(
                [('model', '=', model)], limit=1
            )
            if not ir_model:
                return self._json_response({'error': 'Modelo no encontrado'}, 404)
            fields = request.env['ir.model.fields'].sudo().search_read(
                [('model_id', '=', ir_model.id),
                 ('ttype', 'not in', ['one2many', 'many2many'])],
                ['name', 'field_description', 'ttype', 'relation', 'required', 'readonly'],
            )
            return self._json_response({'model': model, 'fields': fields})
        except Exception as exc:
            return self._json_response({'error': str(exc)}, 500)
