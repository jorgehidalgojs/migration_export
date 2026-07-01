import time
import logging
import requests
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MigrationExportLog(models.Model):
    _name = 'migration.export.log'
    _description = 'Log de Exportación de Migración'
    _order = 'create_date desc'

    model_name = fields.Char(string='Modelo', required=True, index=True)
    last_export_date = fields.Datetime(string='Última Exportación')
    records_exported = fields.Integer(string='Registros Exportados', default=0)
    state = fields.Selection([
        ('idle', 'En espera'),
        ('running', 'Ejecutando'),
        ('error', 'Error'),
        ('done', 'Completado'),
    ], default='idle', tracking=True)
    error_message = fields.Text(string='Mensaje de Error')

    @api.model
    def _cron_push_to_receiver(self):
        """
        Acción programada: envía registros al receptor (v18) por lotes.
        Usa el mismo IMPORT_ORDER que el wizard para mantener consistencia.
        """
        receiver_url = self.env['ir.config_parameter'].sudo().get_param(
            'migration_export.receiver_url'
        )
        receiver_key = self.env['ir.config_parameter'].sudo().get_param(
            'migration_export.receiver_api_key'
        )

        if not receiver_url or not receiver_key:
            _logger.warning("Receptor de migración no configurado. Se omite.")
            return

        from ..controllers.export import EXPORTABLE_MODELS, OPEN_PROCESS_DOMAINS
        # Fuente única de orden — definido en migration_push_wizard
        from .migration_push_wizard import IMPORT_ORDER

        # Resetear logs que quedaron en 'running' de ejecuciones anteriores fallidas
        self.search([('state', '=', 'running')]).write({
            'state': 'error',
            'error_message': 'Reiniciado automáticamente por nueva ejecución del cron',
        })
        self.env.cr.commit()

        for model_name in IMPORT_ORDER:
            if model_name not in EXPORTABLE_MODELS:
                continue
            # Verificar que el modelo existe en esta instalación
            if model_name not in self.env:
                _logger.info("Modelo %s no instalado, se omite.", model_name)
                continue

            log = self.search([('model_name', '=', model_name)], limit=1)
            if not log:
                log = self.create({'model_name': model_name})

            if log.state == 'running':
                _logger.info("Modelo %s ya en ejecución, se omite.", model_name)
                continue

            log.write({'state': 'running'})
            self.env.cr.commit()

            try:
                total = self._push_model_batches(
                    model_name, receiver_url, receiver_key,
                    EXPORTABLE_MODELS[model_name],
                    OPEN_PROCESS_DOMAINS.get(model_name, []),
                )
                log.write({
                    'state': 'done',
                    'last_export_date': fields.Datetime.now(),
                    'records_exported': total,
                    'error_message': False,
                })
            except Exception as exc:
                _logger.exception("Push fallido para %s", model_name)
                log.write({'state': 'error', 'error_message': str(exc)})

            self.env.cr.commit()

    def _push_model_batches(
        self, model_name, receiver_url, receiver_key,
        fields_to_read, domain, batch_size=100
    ):
        """Envía todos los registros de un modelo al receptor en lotes."""
        total_pushed = 0
        offset = 0
        headers = {
            'X-Migration-Key': receiver_key,
            'Content-Type': 'application/json',
        }

        while True:
            records = self.env[model_name].sudo().search_read(
                domain, fields_to_read,
                offset=offset, limit=batch_size, order='id asc',
            )
            if not records:
                break

            resp = requests.post(
                f'{receiver_url}/migration/import/batch',
                json={'model': model_name, 'records': records},
                headers=headers,
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()

            total_pushed += result.get('imported', 0)
            _logger.info(
                "Push %s offset=%d: importados=%d fallidos=%d",
                model_name, offset,
                result.get('imported', 0), result.get('failed', 0),
            )

            if not result.get('has_more', len(records) >= batch_size):
                break

            offset += batch_size
            time.sleep(0.5)

        return total_pushed
