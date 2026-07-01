import json
import logging
import time
import requests
from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Orden de importación respetando dependencias de claves foráneas
IMPORT_ORDER = [
    'uom.category', 'uom.uom',
    'res.lang', 'res.country', 'res.country.state',
    'res.currency', 'res.bank',
    'res.partner.title', 'res.partner.industry', 'res.partner.category',
    'product.category', 'product.tag', 'product.template',
    'account.analytic.group', 'account.analytic.account',
    'account.account', 'account.journal',
    'account.tax', 'account.payment.term',
    'account.incoterms',
    'account.fiscal.position', 'account.fiscal.position.tax',
    'resource.calendar',
    'hr.department', 'hr.job',
    'hr.work.entry.type', 'hr.leave.type',
    'hr.contract.type',
    'hr.payroll.structure.type', 'hr.payroll.structure',
    'crm.team', 'crm.stage', 'crm.tag',
    'stock.warehouse', 'stock.location',
    'stock.picking.type', 'stock.route',
    'res.partner', 'res.partner.bank',
    'product.pricelist', 'product.pricelist.item',
    'product.supplierinfo',
    'hr.employee',
    'project.tags', 'project.project', 'project.task.type',
    'ir.sequence',
    # Procesos abiertos al final
    'sale.order', 'purchase.order',
    'account.move', 'project.task', 'hr.leave',
]

# Campos de líneas para documentos con sublíneas
LINE_CONFIG = {
    'sale.order': ('order_line', [
        'product_id', 'name', 'product_uom_qty', 'price_unit',
        'discount', 'tax_id', 'product_uom', 'sequence',
        'qty_delivered', 'qty_invoiced',
    ]),
    'purchase.order': ('order_line', [
        'product_id', 'name', 'product_qty', 'price_unit',
        'taxes_id', 'product_uom', 'date_planned', 'sequence',
        'qty_received', 'qty_billed',
    ]),
    'account.move': ('invoice_line_ids', [
        'account_id', 'name', 'quantity', 'price_unit',
        'price_subtotal', 'price_total', 'tax_ids',
        'partner_id', 'product_id', 'display_type', 'sequence',
    ]),
}


class MigrationPushWizardModel(models.TransientModel):
    _name = 'migration.push.wizard.model'
    _description = 'Selección de Modelo para Push'
    _order = 'sequence'

    wizard_id = fields.Many2one(
        comodel_name='migration.push.wizard',
        required=True,
        ondelete='cascade',
    )
    sequence = fields.Integer(default=10)
    model_name = fields.Char(string='Modelo', readonly=True)
    record_count = fields.Integer(string='Registros', readonly=True)
    selected = fields.Boolean(string='Exportar', default=True)


class MigrationPushWizard(models.TransientModel):
    _name = 'migration.push.wizard'
    _description = 'Wizard de Push Manual de Migración'

    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Empresa a Migrar',
        required=True,
        default=lambda self: self.env.company,
    )
    target_url = fields.Char(
        string='URL del Receptor (v18)',
        required=True,
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            'migration_export.receiver_url', ''
        ),
    )
    target_api_key = fields.Char(
        string='API Key del Receptor',
        required=True,
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            'migration_export.receiver_api_key', ''
        ),
    )
    batch_size = fields.Integer(
        string='Tamaño de Lote',
        default=100,
        help='Número de registros por lote. Reducir si el servidor se satura.',
    )
    inter_batch_pause = fields.Float(
        string='Pausa entre Lotes (seg)',
        default=0.5,
    )
    result_message = fields.Text(string='Resultado', readonly=True)
    model_line_ids = fields.One2many(
        comodel_name='migration.push.wizard.model',
        inverse_name='wizard_id',
        string='Modelos a Exportar',
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        from ..controllers.export import EXPORTABLE_MODELS, OPEN_PROCESS_DOMAINS

        lines = []
        for seq, model_name in enumerate(IMPORT_ORDER, start=10):
            if model_name not in EXPORTABLE_MODELS:
                continue
            try:
                domain = list(OPEN_PROCESS_DOMAINS.get(model_name, []))
                Model = self.env[model_name]
                if 'company_id' in Model._fields:
                    domain.append(('company_id', '=', self.env.company.id))
                count = Model.sudo().search_count(domain)
            except Exception:
                count = 0
            lines.append((0, 0, {
                'model_name': model_name,
                'record_count': count,
                'selected': True,
                'sequence': seq,
            }))

        res['model_line_ids'] = lines
        return res

    @api.onchange('company_id')
    def _onchange_company_id(self):
        """Actualiza los conteos de registros al cambiar de empresa."""
        from ..controllers.export import EXPORTABLE_MODELS, OPEN_PROCESS_DOMAINS

        for line in self.model_line_ids:
            try:
                domain = list(OPEN_PROCESS_DOMAINS.get(line.model_name, []))
                Model = self.env[line.model_name]
                if 'company_id' in Model._fields:
                    domain.append(('company_id', '=', self.company_id.id))
                line.record_count = Model.sudo().search_count(domain)
            except Exception:
                line.record_count = 0

    def action_push_now(self):
        self.ensure_one()
        if not self.target_url or not self.target_api_key:
            raise UserError(
                "Configure la URL del receptor y la API Key antes de continuar."
            )

        selected = self.model_line_ids.filtered('selected').mapped('model_name')
        if not selected:
            raise UserError("Seleccione al menos un modelo para exportar.")

        # Validar conectividad antes de empezar
        self._ping_receiver()

        headers = {
            'X-Migration-Key': self.target_api_key,
            'Content-Type': 'application/json',
        }
        company_id = self.company_id.id
        company_name = self.company_id.name

        total = 0
        errors = []
        Log = self.env['migration.export.log']

        for model_name in IMPORT_ORDER:
            if model_name not in selected:
                continue
            if model_name not in self.env:
                continue

            # Crear/actualizar entrada de log visible para el usuario
            log = Log.search([('model_name', '=', model_name)], limit=1)
            if not log:
                log = Log.create({'model_name': model_name})
            log.write({'state': 'running', 'error_message': False})
            self.env.cr.commit()

            try:
                pushed = self._push_model(
                    model_name, company_id, company_name, headers
                )
                total += pushed
                log.write({
                    'state': 'done',
                    'records_exported': pushed,
                    'last_export_date': fields.Datetime.now(),
                })
                self.env.cr.commit()
                _logger.info("Push %s: %d registros enviados", model_name, pushed)
            except Exception as exc:
                err = f"{model_name}: {exc}"
                errors.append(err)
                log.write({'state': 'error', 'error_message': str(exc)})
                self.env.cr.commit()
                _logger.error("Error empujando %s: %s", model_name, exc)

        msg_type = 'danger' if errors else 'success'
        msg = f"Push completado: {total} registros enviados."
        if errors:
            msg += f" Errores en: {', '.join(e.split(':')[0] for e in errors[:5])}"

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Migración Push',
                'message': msg,
                'type': msg_type,
                'sticky': True,
                'next': {
                    'type': 'ir.actions.act_window',
                    'name': 'Log de Exportación',
                    'res_model': 'migration.export.log',
                    'view_mode': 'tree,form',
                    'target': 'current',
                },
            },
        }

    def _ping_receiver(self):
        """Verifica conectividad con el receptor antes del push."""
        try:
            resp = requests.get(
                f'{self.target_url.rstrip("/")}/migration/import/ping',
                timeout=10,
            )
            if resp.status_code != 200:
                raise UserError(
                    f"El receptor respondió con código {resp.status_code}. "
                    f"Verifique la URL: {self.target_url}"
                )
        except requests.ConnectionError:
            raise UserError(
                f"No se pudo conectar con el receptor en:\n{self.target_url}\n\n"
                "Verifique que la URL es correcta y el servidor está accesible."
            )
        except requests.Timeout:
            raise UserError(
                f"Tiempo de espera agotado conectando a:\n{self.target_url}"
            )

    def _push_model(
        self, model_name, company_id, company_name, headers
    ):
        from ..controllers.export import EXPORTABLE_MODELS, OPEN_PROCESS_DOMAINS

        fields_to_read = EXPORTABLE_MODELS[model_name]
        domain = list(OPEN_PROCESS_DOMAINS.get(model_name, []))
        Model = self.env[model_name]

        if 'company_id' in Model._fields:
            domain.append(('company_id', '=', company_id))

        offset = 0
        total = 0
        has_lines = model_name in LINE_CONFIG

        while True:
            records = Model.sudo().search_read(
                domain, fields_to_read,
                offset=offset, limit=self.batch_size, order='id asc',
            )
            if not records:
                break

            # Embeber líneas si el modelo las tiene
            if has_lines:
                records = self._embed_lines(Model, model_name, records)

            payload = json.dumps({
                'model': model_name,
                'source_company_id': company_id,
                'source_company_name': company_name,
                'records': records,
            }, default=str)

            resp = requests.post(
                f'{self.target_url}/migration/import/batch',
                headers=headers,
                data=payload,
                timeout=120,
            )
            resp.raise_for_status()
            result = resp.json()
            total += result.get('imported', 0)

            # Usar has_more del receptor para decidir si continuar
            if not result.get('has_more', len(records) >= self.batch_size):
                break

            offset += self.batch_size
            time.sleep(self.inter_batch_pause)

        return total

    def _embed_lines(self, Model, model_name, records):
        """Añade las líneas embebidas a cada registro del lote."""
        line_field, line_fields = LINE_CONFIG[model_name]
        record_ids = [r['id'] for r in records]
        parents = Model.sudo().browse(record_ids)

        # Leer todas las líneas en una sola consulta (evita N+1)
        all_line_ids = []
        parent_to_line_ids = {}
        for parent in parents:
            line_recs = getattr(parent, line_field)
            parent_to_line_ids[parent.id] = line_recs.ids
            all_line_ids.extend(line_recs.ids)

        lines_by_id = {}
        if all_line_ids:
            line_model_name = Model._fields[line_field].comodel_name
            all_lines = self.env[line_model_name].sudo().browse(all_line_ids)
            for line_data in all_lines.read(line_fields):
                lines_by_id[line_data['id']] = line_data

        for rec in records:
            rec['_lines'] = [
                lines_by_id[lid]
                for lid in parent_to_line_ids.get(rec['id'], [])
                if lid in lines_by_id
            ]
            rec['_line_field'] = line_field

        return records
