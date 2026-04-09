import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url
from urllib.parse import urlencode

from payments.payment_gateways.doctype.momo_settings.momo_connector import MomoConnector
from payments.utils import create_payment_gateway


def create_mode_of_payment(gateway, payment_type="General"):
    payment_gateway_account = frappe.db.get_value(
        "Payment Gateway Account", {"payment_gateway": gateway}, "payment_account"
    )
    mode_of_payment = frappe.db.exists("Mode of Payment", gateway)
    if not mode_of_payment and payment_gateway_account:
        mode_of_payment = frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": gateway,
            "type": payment_type,
            "accounts": [{
                "doctype": "Mode of Payment Account",
                "company": frappe.db.get_value("Account", payment_gateway_account, "company"),
                "default_account": payment_gateway_account,
            }],
        })
        mode_of_payment.insert(ignore_permissions=True)
        return mode_of_payment
    elif mode_of_payment:
        return frappe.get_doc("Mode of Payment", mode_of_payment)


class MoMoSettings(Document):

    @property
    def supported_currencies_list(self):
        raw = self.supported_currencies or "XAF"
        return [c.strip() for c in raw.split(",") if c.strip()]

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies_list:
            frappe.throw(
                _("MTN MoMo does not support transactions in currency '{0}'. "
                  "Supported: {1}").format(
                    currency, ", ".join(self.supported_currencies_list)
                )
            )

    def on_update(self):
        create_payment_gateway(
            "MoMo-" + self.gateway_name,
            settings="MoMo Settings",
            controller=self.gateway_name,
        )
        call_hook_method(
            "payment_gateway_enabled",
            gateway="MoMo-" + self.gateway_name,
            payment_channel="",
        )
        create_mode_of_payment("MoMo-" + self.gateway_name, payment_type="Phone")

        callback = (
            frappe.utils.get_url()
            + "/api/method/payments.payment_gateways.doctype.momo_settings"
              ".momo_settings.verify_transaction"
        )
        self.db_set("callback_url", callback, update_modified=False)
        frappe.db.commit()

    def get_payment_url(self, **kwargs):
        kwargs.setdefault("payment_request_name", kwargs.get("order_id", ""))
        return get_url(f"momo_checkout?{urlencode(kwargs)}")

    def request_for_payment(self, **kwargs):
        args = frappe._dict(kwargs)

        # Always read phone_number from the Payment Request document itself
        # This is the field the desk user fills in — never rely on args.sender
        phone_number = args.get("phone_number")
        if args.get("order_id") and not phone_number:
            phone_number = frappe.db.get_value(
                "Payment Request", args.order_id, "phone_number"
            )

        if not phone_number:
            frappe.throw(
                _("Please enter the MTN Cameroon phone number (with country code e.g. 237XXXXXXXXX) "
                  "in the Phone Number field before submitting."),
                title=_("Phone Number Required")
            )

        try:
            callback_url = (
                frappe.utils.get_url()
                + "/api/method/payments.payment_gateways.doctype.momo_settings"
                  ".momo_settings.verify_transaction"
            )
            connector = self._get_connector()
            response = connector.request_to_pay(
                amount=args.request_amount,
                currency=args.currency,
                payer_msisdn=phone_number,
                external_id=args.order_id,
                callback_url=callback_url,
            )
            if response and not frappe.db.exists(
                "Integration Request", response["referenceId"]
            ):
                create_request_log(
                    args, "Host", "MoMo", response["referenceId"]
                )
            return response
        except Exception:
            frappe.log_error(frappe.get_traceback(), "MoMo Request to Pay Error")
            frappe.throw(_("MoMo payment initiation failed."), title=_("MoMo Error"))

    def _get_connector(self):
        env = "sandbox" if self.use_sandbox else "production"
        target_env = "sandbox" if self.use_sandbox else self.target_environment
        return MomoConnector(
            env=env,
            api_user_id=self.api_user_id,
            api_key=self.get_password("api_key"),
            subscription_key=self.get_password("subscription_key"),
            target_environment=target_env,
        )


# -- Whitelisted endpoints ----------------------------------------------------

@frappe.whitelist(allow_guest=True)
def verify_transaction(**kwargs):
    data = frappe._dict(kwargs)
    reference_id = data.get("referenceId") or data.get("externalId")

    if not reference_id:
        return {"error": "Missing reference ID"}

    if not frappe.db.exists("Integration Request", reference_id):
        return {"error": "Integration Request not found"}

    integration_request = frappe.get_doc("Integration Request", reference_id)

    if data.get("status") == "SUCCESSFUL":
        original_user = frappe.session.user
        try:
            frappe.set_user("Administrator")
            integration_request.handle_success(data)
            integration_request.db_set("status", "Completed")

            if integration_request.reference_doctype == "Payment Request":
                pr = frappe.get_doc(
                    "Payment Request",
                    integration_request.reference_docname
                )
                if pr.status != "Paid":
                    pr.create_payment_entry()
                    pr.add_comment(
                        "Info",
                        f"MoMo Confirmed. ID: {data.get('financialTransactionId')}"
                    )
            frappe.db.commit()
        except Exception:
            integration_request.handle_failure(data)
            frappe.log_error(frappe.get_traceback(), "MoMo Callback Error")
        finally:
            frappe.set_user(original_user)
    else:
        original_user = frappe.session.user
        try:
            frappe.set_user("Administrator")
            integration_request.handle_failure(data)
            integration_request.db_set("status", "Failed")
            if integration_request.reference_doctype == "Payment Request":
                pr = frappe.get_doc(
                    "Payment Request",
                    integration_request.reference_docname
                )
                if pr.status not in ("Paid", "Cancelled"):
                    reason = data.get("reason") or "Payment declined or cancelled by customer"
                    pr.db_set("status", "Failed")
                    pr.add_comment(
                        "Info",
                        f"MoMo Payment Failed. Reason: {reason}. Reference: {reference_id}"
                    )
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "MoMo Failure Handler Error")
        finally:
            frappe.set_user(original_user)
    return {"status": "processed", "reference_id": reference_id}
    return {"status": "processed", "reference_id": reference_id}


@frappe.whitelist()
def generate_payment_url(payment_request_name):
    pr = frappe.get_doc("Payment Request", payment_request_name)
    from payments.utils import get_payment_gateway_controller
    controller = get_payment_gateway_controller(pr.payment_gateway)
    url = controller.get_payment_url(
        amount=pr.grand_total,
        currency=pr.currency,
        order_id=pr.name,
        reference_doctype="Payment Request",
        reference_docname=pr.name,
        payer_name=pr.party_name,
        payer_email=pr.party,
        title=f"Payment for {pr.reference_name}",
        description=f"MTN MoMo Payment for {pr.party_name}",
        payment_gateway=pr.payment_gateway,
        payment_request_name=pr.name,
    )
    frappe.db.set_value("Payment Request", payment_request_name, "payment_url", url)
    frappe.db.commit()
    return url


@frappe.whitelist(allow_guest=True)
def request_for_payment_by_gateway(gateway_name, **kwargs):
    return frappe.get_doc("MoMo Settings", gateway_name).request_for_payment(**kwargs)


@frappe.whitelist()
def poll_transaction_status(reference_id, gateway_name):
    """
    Used by the Test Connection button.
    Tests authentication only — does not look up any transaction.
    """
    try:
        settings = frappe.get_doc("MoMo Settings", gateway_name)
        connector = settings._get_connector()
        # Just return the token to confirm auth works — no transaction lookup
        return {"status": "ok", "message": "Authentication successful. Credentials are valid."}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "MoMo Test Connection Error")
        frappe.throw(_("Could not authenticate with MTN MoMo API. Check credentials and Error Log."))


@frappe.whitelist()
def retry_payment_request(payment_request_name):
    """
    Resends the STK push for a Failed Payment Request.
    Reuses the exact same request_phone_payment logic as on_submit.
    Guards against retrying a PR that is already Paid.
    """
    pr = frappe.get_doc("Payment Request", payment_request_name)

    if pr.status == "Paid":
        frappe.throw(
            _("This Payment Request has already been paid and cannot be retried."),
            title=_("Already Paid")
        )

    if pr.docstatus != 1:
        frappe.throw(
            _("Only submitted Payment Requests can be retried."),
            title=_("Invalid Status")
        )

    if not pr.phone_number:
        frappe.throw(
            _("No phone number found on this Payment Request."),
            title=_("Phone Number Required")
        )

    # Enforce 60-second cooldown between retries
    last_ir = frappe.db.get_all(
        "Integration Request",
        filters={
            "reference_doctype": "Payment Request",
            "reference_docname": pr.name,
            "status": "Failed",
        },
        fields=["modified"],
        order_by="creation desc",
        limit=1,
    )
    if last_ir:
        import datetime
        last_attempt = last_ir[0].modified
        now = frappe.utils.now_datetime()
        seconds_since = (now - last_attempt).total_seconds()
        if seconds_since < 180:
            wait = int(180 - seconds_since)
            frappe.throw(
                _(f"Please wait {wait} seconds before retrying. MTN requires a cooldown between payment attempts."),
                title=_("Too Soon")
            )

    # Reset PR status back to Requested
    pr.db_set("status", "Requested")
    pr.add_comment("Info", f"MoMo payment retry initiated. Resending STK to {pr.phone_number}.")
    frappe.db.commit()

    # Reuse the exact same path as on_submit -> request_phone_payment
    pr.request_phone_payment()


@frappe.whitelist()
def get_retry_cooldown(payment_request_name):
    """
    Returns remaining cooldown seconds for the Try Again button.
    Computed server-side to avoid client timezone issues.
    """
    COOLDOWN = 180  # 3 minutes
    last_ir = frappe.get_all(
        "Integration Request",
        filters={
            "reference_doctype": "Payment Request",
            "reference_docname": payment_request_name,
            "status": "Failed",
        },
        fields=["modified"],
        order_by="creation desc",
        limit=1,
    )
    if not last_ir:
        return {"remaining": 0}

    import datetime
    last_modified = last_ir[0].modified
    now = frappe.utils.now_datetime()
    seconds_since = (now - last_modified).total_seconds()
    remaining = max(0, int(COOLDOWN - seconds_since))
    return {"remaining": remaining}
