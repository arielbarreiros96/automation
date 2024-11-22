# Copyright 2024 Dixmit
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AutomationRecord(models.Model):

    _name = "automation.record"
    _description = "Automation Record"

    name = fields.Char(compute="_compute_name")
    is_orphaned = fields.Boolean(
        compute="_compute_is_orphaned",
        store=False,
    )
    is_placeholder = fields.Boolean(default=False)
    orphaned_id = fields.Integer()
    state = fields.Selection(
        [("run", "Running"), ("done", "Done"), ("orphaned", "Orphaned")],
        compute="_compute_state",
        store=True,
    )
    configuration_id = fields.Many2one(
        "automation.configuration", required=True, readonly=True
    )
    model = fields.Char(index=True, required=False, readonly=True)
    resource_ref = fields.Reference(
        selection="_selection_target_model",
        compute="_compute_resource_ref",
        readonly=True,
    )
    res_id = fields.Many2oneReference(
        string="Record",
        index=True,
        required=False,
        readonly=True,
        model_field="model",
        copy=False,
    )
    automation_step_ids = fields.One2many(
        "automation.record.step", inverse_name="record_id", readonly=True
    )
    is_test = fields.Boolean()

    @api.model
    def _selection_target_model(self):
        return [
            (model.model, model.name)
            for model in self.env["ir.model"]
            .sudo()
            .search([("is_mail_thread", "=", True)])
        ]

    @api.depends("automation_step_ids.state", "is_orphaned")
    def _compute_state(self):
        for record in self:
            if record.is_orphaned:
                record.state = "orphaned"
            else:
                scheduled_steps = record.automation_step_ids.filtered(
                    lambda r: r.state == "scheduled"
                )
                record.state = "run" if scheduled_steps else "done"

    @api.depends("model", "res_id")
    def _compute_resource_ref(self):
        for record in self:
            if record.model and record.model in self.env:
                record.resource_ref = "%s,%s" % (record.model, record.res_id or 0)
            else:
                record.resource_ref = None

    @api.depends("res_id", "model")
    def _compute_name(self):
        for record in self:
            record.name = self.env[record.model].browse(record.res_id).display_name

    @api.depends("res_id", "model")
    def _compute_is_orphaned(self):
        for record in self:
            record.is_orphaned = not bool(
                self.env[record.model].browse(record.res_id).exists()
            )

    @api.model
    def _search(
        self,
        args,
        offset=0,
        limit=None,
        order=None,
        count=False,
        access_rights_uid=None,
    ):
        ids = super()._search(
            args,
            offset=offset,
            limit=limit,
            order=order,
            count=False,
            access_rights_uid=access_rights_uid,
        )
        if not ids:
            return 0 if count else []

        model_data = defaultdict(set)  # {res_model: set(res_id)}
        for res_id, model in self.browse(ids).mapped(
            lambda r: (r.id, r.res_id, r.model)
        ):
            model_data[model].add(res_id)

        orphaned_ids = set()
        for model, res_ids in model_data.items():
            existing_ids = set(self.env[model].browse(list(res_ids)).exists().ids)
            orphaned_ids |= set(res_ids) - existing_ids

        existing_placeholders = self.browse(ids).filtered(
            lambda r: r.is_placeholder and r.orphaned_id
        )

        # Construir el map de placeholders usando los registros ya disponibles
        placeholders_map = {
            placeholder.orphaned_id: placeholder.id
            for placeholder in existing_placeholders
        }

        placeholder_ids = []
        for record in self.browse(ids).filtered(
            lambda r: r.res_id in orphaned_ids and not r.is_placeholder
        ):
            if record.id in placeholders_map:
                # Existing placeholder found, add its ID
                placeholder_ids.append(placeholders_map[record.id])
            else:
                # Create a new placeholder if not already linked
                placeholder = self.create(
                    {
                        "name": _(f"[Orphaned Record - ID {record.id}]"),
                        "orphaned_id": record.id,
                        "is_orphaned": True,
                        "is_test": False,
                        "model": record.model,
                        "res_id": None,
                        "state": "orphaned",
                        "configuration_id": record.configuration_id.id,
                        "is_placeholder": True,
                    }
                )
                placeholder_ids.append(placeholder.id)

        valid_ids = [
            rec_id
            for rec_id, res_id, model in self.browse(ids).mapped(
                lambda r: (r.id, r.res_id, r.model)
            )
            if res_id not in orphaned_ids
        ]

        valid_ids.extend(placeholder_ids)

        return len(valid_ids) if count else valid_ids

    def read(self, fields=None, load="_classic_read"):
        """Override to explicitely call check_access_rule, that is not called
        by the ORM. It instead directly fetches ir.rules and apply them."""
        self.check_access_rule("read")
        return super().read(fields=fields, load=load)

    def check_access_rule(self, operation):
        """In order to check if we can access a record, we are checking if we can access
        the related document"""
        super().check_access_rule(operation)
        if self.env.is_superuser():
            return
        default_checker = self.env["mail.thread"].get_automation_access
        by_model_rec_ids = defaultdict(set)
        by_model_checker = {}
        for exc_rec in self.sudo():
            by_model_rec_ids[exc_rec.model].add(exc_rec.res_id)
            if exc_rec.model not in by_model_checker:
                by_model_checker[exc_rec.model] = getattr(
                    self.env[exc_rec.model], "get_automation_access", default_checker
                )

        for model, rec_ids in by_model_rec_ids.items():
            records = self.env[model].browse(rec_ids).with_user(self._uid)
            checker = by_model_checker[model]
            for record in records:
                check_operation = checker(
                    [record.id], operation, model_name=record._name
                )
                record.check_access_rights(check_operation)
                record.check_access_rule(check_operation)

    def write(self, vals):
        self.check_access_rule("write")
        if any(self.filtered("is_orphaned")):
            raise UserError("You cannot modify orphaned records.")
        return super().write(vals)

    def unlink(self):
        if any(self.filtered("is_orphaned")):
            raise UserError("You cannot delete orphaned records.")
        return super().unlink()
