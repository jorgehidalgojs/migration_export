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


class MigrationPushModelLine(models.Model):
    """Línea de selección de modelo — persistente para no perder estado."""
    _name = 'migration.push.model.line'
    _description = 'Línea de Modelo para Push'
    _order = 'sequence'

    config_id = fields.Many2one(
        comodel_name='migration.push.config',
        required=True,
        ondelete='cascade',
    )
    sequence = fields.Integer(default=10)
    model_name = fields.Char(string='Modelo', readonly=True)
    record_count = fields.Integer(string='Registros', readonly=True)
    selected = fields.Boolean(string='Exportar', default=True)


class MigrationPushConfig(models.Model):
    """Configuración persistente del push — no TransientModel, evita problemas de diálogo."""
    _name = 'migration.push.config'
    _description = 'Configuración de Push de Migración'

    name = fields.Char(default='Configuración Push', readonly=True)
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Empresa a Migrar',
        required=True,
        default=lambda self: self.env.company,
    )
    target_url = fields.Char(
        string='URL del Receptor (v18)',
        help='Ejemplo: https://mi-odoo18.ejemplo.com',
    )
    target_api_key = fields.Char(
        string='API Key del Receptor',
        password=True,
    )
    batch_size = fields.Integer(
        string='Tamaño de Lote',
        default=100,
    )
    inter_batch_pause = fields.Float(
        string='Pausa entre Lotes (seg)',
        default=0.5,
    )
    connection_status = fields.Char(
        string='Estado de Conexión',
        readonly=True,
    )
    result_message = fields.Text(
        string='Resultado del último push',
        readonly=True,
    )
    model_line_ids = fields.One2many(
        comodel_name='migration.push.model.line',
        inverse_name='config_id',
        string='Modelos a Exportar',
    )

    @api.model
    def get_or_create_singleton(self):
        """Devuelve el único registro de configuración, creándolo si no existe."""
        cfg = self.search([], limit=1)
        if not cfg:
            cfg = self.create({'name': 'Configuración Push'})
            cfg._rebuild_model_lines()
        return cfg

    def _rebuild_model_lines(self):
        """(Re)construye las líneas de modelos con sus conteos."""
        from ..controllers.export import EXPORTABLE_MODELS, OPEN_PROCESS_DOMAINS

        self.model_line_ids.unlink()
        lines = []
        for seq, model_name in enumerate(IMPORT_ORDER, start=10):
            if model_name not in EXPORTABLE_MODELS:
                continue
            if model_name not in self.env:
                continue
            try:
                domain = list(OPEN_PROCESS_DOMAINS.get(model_name, []))
                Model = self.env[model_name]
                if 'company_id' in Model._fields:
                    domain.append(('company_id', '=', self.company_id.id))
                count = Model.sudo().search_count(domain)
            except Exception:
                count = 0
            lines.append({
                'config_id': self.id,
                'model_name': model_name,
                'record_count': count,
                'selected': True,
                'sequence': seq,
            })
        self.env['migration.push.model.line'].create(lines)

    # ------------------------------------------------------------------
    # Botones de la vista
    # ------------------------------------------------------------------

    def action_rebuild_list(self):
        """Reconstruye la lista de modelos actualizando los conteos."""
        self.ensure_one()
        self._rebuild_model_lines()
        self.connection_status = False
        self.result_message = False

    def action_select_all(self):
        self.ensure_one()
        self.model_line_ids.write({'selected': True})

    def action_deselect_all(self):
        self.ensure_one()
        self.model_line_ids.write({'selected': False})

    def action_test_connection(self):
        """Prueba la conexión con el receptor v18 y muestra el resultado."""
        self.ensure_one()
        if not self.target_url:
            self.connection_status = '❌ URL del receptor no configurada'
            return

        url = self.target_url.rstrip('/')
        try:
            resp = requests.get(
                f'{url}/migration/import/ping',
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                version = data.get('version', '?')
                self.connection_status = (
                    f'✅ Conexión OK — Receptor Odoo {version} responde en {url}'
                )
            else:
                self.connection_status = (
                    f'⚠️ El receptor respondió con código HTTP {resp.status_code}'
                )
        except requests.ConnectionError:
            self.connection_status = (
                f'❌ No se pudo conectar a {url} — '
                'verifique que la URL es correcta y el servidor está accesible'
            )
        except requests.Timeout:
            self.connection_status = f'❌ Tiempo de espera agotado conectando a {url}'
        except Exception as exc:
            self.connection_status = f'❌ Error: {exc}'

    def action_push_now(self):
        """Ejecuta el push de los modelos seleccionados al receptor v18."""
        self.ensure_one()

        if not self.target_url or not self.target_api_key:
            raise UserError(
                'Configure la URL del receptor y la API Key antes de continuar.'
            )

        selected = self.model_line_ids.filtered('selected').mapped('model_name')
        if not selected:
            raise UserError('Seleccione al menos un modelo para exportar.')

        # Test de conexión previo
        self.action_test_connection()
        self.env.cr.commit()
        if self.connection_status and self.connection_status.startswith('❌'):
            raise UserError(self.connection_status)

        headers = {
            'X-Migration-Key': self.target_api_key,
            'Content-Type': 'application/json',
        }
        company_id = self.company_id.id
        company_name = self.company_id.name

        totals = {'imported': 0, 'skipped': 0, 'failed': 0}
        errors = []
        Log = self.env['migration.export.log']

        for model_name in IMPORT_ORDER:
            if model_name not in selected:
                continue
            if model_name not in self.env:
                continue

            log = Log.search([('model_name', '=', model_name)], limit=1)
            if not log:
                log = Log.create({'model_name': model_name})
            log.write({'state': 'running', 'error_message': False})
            self.env.cr.commit()

            try:
                stats = self._push_model(model_name, company_id, company_name, headers)
                for k in totals:
                    totals[k] += stats.get(k, 0)
                processed = sum(stats.get(k, 0) for k in ('imported', 'skipped', 'failed'))
                log.write({
                    'state': 'done',
                    'records_exported': processed,
                    'last_export_date': fields.Datetime.now(),
                })
                self.env.cr.commit()
                _logger.info(
                    'Push %s: importados=%d omitidos=%d fallidos=%d',
                    model_name, stats.get('imported', 0),
                    stats.get('skipped', 0), stats.get('failed', 0),
                )
            except Exception as exc:
                errors.append(f'{model_name}: {exc}')
                log.write({'state': 'error', 'error_message': str(exc)})
                self.env.cr.commit()
                _logger.error('Error empujando %s: %s', model_name, exc)

        self.result_message = (
            f'Push completado {fields.Datetime.now()}:\n'
            f'  • Importados/actualizados: {totals["imported"]}\n'
            f'  • Ya existían (omitidos):  {totals["skipped"]}\n'
            f'  • Con error:               {totals["failed"]}'
        )
        if errors:
            self.result_message += '\n\nModelos con error:\n' + '\n'.join(
                f'  - {e}' for e in errors[:10]
            )
        self.env.cr.commit()

    # ------------------------------------------------------------------
    # Push interno
    # ------------------------------------------------------------------

    def _push_model(self, model_name, company_id, company_name, headers):
        from ..controllers.export import EXPORTABLE_MODELS, OPEN_PROCESS_DOMAINS

        fields_to_read = EXPORTABLE_MODELS[model_name]
        domain = list(OPEN_PROCESS_DOMAINS.get(model_name, []))
        Model = self.env[model_name]

        if 'company_id' in Model._fields:
            domain.append(('company_id', '=', company_id))

        offset = 0
        totals = {'imported': 0, 'skipped': 0, 'failed': 0}
        has_lines = model_name in LINE_CONFIG

        while True:
            records = Model.sudo().search_read(
                domain, fields_to_read,
                offset=offset, limit=self.batch_size, order='id asc',
            )
            if not records:
                break

            if has_lines:
                records = self._embed_lines(Model, model_name, records)

            payload = json.dumps({
                'model': model_name,
                'source_company_id': company_id,
                'source_company_name': company_name,
                'records': records,
            }, default=str)

            resp = requests.post(
                f'{self.target_url.rstrip("/")}/migration/import/batch',
                headers=headers,
                data=payload,
                timeout=120,
            )
            resp.raise_for_status()
            result = resp.json()

            if 'error' in result:
                raise Exception(result['error'])

            for k in totals:
                totals[k] += result.get(k, 0)

            if len(records) < self.batch_size:
                break

            offset += self.batch_size
            time.sleep(self.inter_batch_pause)

        return totals

    def _embed_lines(self, Model, model_name, records):
        line_field, line_fields = LINE_CONFIG[model_name]
        record_ids = [r['id'] for r in records]
        parents = Model.sudo().browse(record_ids)

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
