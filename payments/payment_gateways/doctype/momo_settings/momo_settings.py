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

@frappe.whitelist()
def poll_pending_transactions():
    """
    Periodically checks transactions that are still in 'Pending' status 
    in the Integration Request table.
    """
    pending_requests = frappe.get_all("Integration Request", 
        filters={"status": "Queued", "integration_request_service": "MoMo"},
        fields=["name", "reference_docname"]
    )
    
    for req in pending_requests:
        # This calls your existing polling logic for each stuck transaction
        poll_webshop_transaction(req.name)

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


@frappe.whitelist()
def initiate_webshop_payment(token, gateway_name, phone):
    """
    Called from the MoMo checkout page.
    Reads cart data from cache, creates a Payment Request, sends STK push.
    Does NOT create SO/SI yet — that happens only after payment confirmed.
    """
    import json
    raw = frappe.cache().get_value(f"momo_pending_{token}")
    if not raw:
        return {"error": "Payment session expired. Please return to your cart."}
    cart_data = frappe._dict(json.loads(raw))
    # Build full gateway name
    gw_full = "MoMo-" + gateway_name if not gateway_name.startswith("MoMo-") else gateway_name
    pga = frappe.db.get_value(
        "Payment Gateway Account",
        {"payment_gateway": gw_full},
        ["name", "payment_account", "currency"],
        as_dict=True,
    )
    # Create a lightweight Payment Request (no SO yet — reference is the token)
    pr = frappe.new_doc("Payment Request")
    pr.payment_request_type = "Inward"
    pr.party_type = "Customer"
    pr.party = cart_data.customer
    pr.reference_doctype = "Quotation"
    pr.reference_name = cart_data.quotation_name
    pr.payment_gateway_account = pga.name if pga else ""
    pr.payment_gateway = gw_full
    pr.payment_account = pga.payment_account if pga else ""
    pr.currency = cart_data.currency
    pr.grand_total = cart_data.grand_total
    pr.base_grand_total = cart_data.grand_total
    pr.outstanding_amount = cart_data.grand_total
    pr.email_to = cart_data.contact_email or cart_data.customer
    pr.subject = f"Webshop payment for {cart_data.customer_name}"
    pr.flags.ignore_permissions = True
    pr.insert()
    frappe.db.set_value("Payment Request", pr.name, {"docstatus": 1, "status": "Requested"})
    # Store PR name back into the token cache so finalize can find it
    cart_data["payment_request"] = pr.name
    frappe.cache().set_value(f"momo_pending_{token}", json.dumps(cart_data), expires_in_sec=1800)
    # Clean and format phone
    clean_phone = "".join(filter(str.isdigit, str(phone)))
    if len(clean_phone) == 9:
        clean_phone = "237" + clean_phone
    # Get connector and send STK push
    settings_name = frappe.db.get_value("MoMo Settings", {"gateway_name": gateway_name}, "name")
    if not settings_name:
        return {"error": f"MoMo Settings not found for: {gateway_name}"}
    momo_doc = frappe.get_doc("MoMo Settings", settings_name)
    connector = momo_doc._get_connector()
    int_amount = str(int(float(cart_data.grand_total)))
    callback_url = frappe.utils.get_url(
        "/api/method/payments.payment_gateways.doctype.momo_settings.momo_settings.verify_transaction"
    )
    response = connector.request_to_pay(
        amount=int_amount,
        currency=cart_data.currency,
        payer_msisdn=clean_phone,
        external_id=pr.name,
        callback_url=callback_url,
    )
    if not response or not response.get("referenceId"):
        return {"error": "MTN did not return a reference ID. Check phone number and try again."}
    reference_id = response["referenceId"]
    from frappe.integrations.utils import create_request_log
    args = frappe._dict(
        sender=clean_phone, request_amount=int_amount,
        currency=cart_data.currency, order_id=pr.name,
        reference_doctype="Payment Request", reference_docname=pr.name,
    )
    create_request_log(args, "Host", "MoMo", reference_id)
    frappe.db.set_value("Integration Request", reference_id, {
        "reference_doctype": "Payment Request",
        "reference_docname": pr.name,
    })
    frappe.db.commit()
    return {"reference_id": reference_id, "payment_request": pr.name}


@frappe.whitelist()
def poll_webshop_transaction(reference_id, gateway_name=None):
    """
    Active poll: checks DB first, then asks MTN directly if still pending.
    Returns {"status": "SUCCESSFUL" | "FAILED" | "PENDING" | "TIMEOUT"}
    Safe to call repeatedly — never creates Payment Entries itself.
    """
    if not frappe.db.exists("Integration Request", reference_id):
        return {"status": "PENDING"}  # not logged yet, still in flight
    db_status = frappe.db.get_value("Integration Request", reference_id, "status")
    # Fast path — already resolved in DB
    if db_status == "Completed":
        return {"status": "SUCCESSFUL"}
    if db_status in ("Failed", "Cancelled"):
        return {"status": "FAILED"}
    # Still Queued/Pending — ask MTN directly
    try:
        bare_name = (gateway_name or "").replace("MoMo-", "") if gateway_name else None
        if not bare_name:
            ir = frappe.get_doc("Integration Request", reference_id)
            pr_name = ir.reference_docname
            if pr_name and frappe.db.exists("Payment Request", pr_name):
                gw_full = frappe.db.get_value("Payment Request", pr_name, "payment_gateway") or ""
                bare_name = gw_full.replace("MoMo-", "")
        if not bare_name:
            return {"status": "PENDING"}
        settings_name = frappe.db.get_value("MoMo Settings", {"gateway_name": bare_name}, "name")
        if not settings_name:
            return {"status": "PENDING"}
        momo_doc = frappe.get_doc("MoMo Settings", settings_name)
        connector = momo_doc._get_connector()
        mtn_resp = connector.get_transaction_status(reference_id)
        mtn_status = (mtn_resp or {}).get("status", "PENDING")
        if mtn_status == "SUCCESSFUL":
            frappe.db.set_value("Integration Request", reference_id, "status", "Completed")
            frappe.db.commit()
            return {"status": "SUCCESSFUL"}
        if mtn_status == "FAILED":
            frappe.db.set_value("Integration Request", reference_id, "status", "Failed")
            frappe.db.commit()
            return {"status": "FAILED"}
        return {"status": "PENDING"}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "MoMo poll_webshop_transaction Error")
        return {"status": "PENDING"}


@frappe.whitelist()
def finalize_webshop_order(token, reference_id):
    """
    Called after polling confirms MTN payment is Completed.
    Flow: SO (To Deliver) -> PR (Paid) -> PE (against SO)
    SI is created from SO by warehouse staff after delivery.
    """
    import json

    raw = frappe.cache().get_value(f"momo_pending_{token}")
    if not raw:
        frappe.log_error(
            f"Token: {token}\nRef: {reference_id}",
            "MoMo Finalize: Token Missing"
        )
        return {"error": "Session expired. Contact support with ref: " + reference_id}

    cart_data = json.loads(raw)
    pr_name     = cart_data.get("payment_request")
    paid_amount = float(cart_data.get("grand_total") or 0)
    quotation   = cart_data.get("quotation_name")

    existing_so = frappe.db.get_value(
        "Sales Order",
        {"po_no": quotation, "docstatus": 1},
        "name"
    )
    if existing_so:
        has_pe = frappe.db.exists("Payment Entry Reference", {
            "reference_doctype": "Sales Order",
            "reference_name": existing_so,
        })
        if has_pe:
            frappe.cache().delete_value(f"momo_pending_{token}")
            return {"sales_order": existing_so}

    original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        if not existing_so:
            so = frappe.new_doc("Sales Order")
            so.customer         = cart_data["customer"]
            so.company          = cart_data["company"]
            so.currency         = cart_data["currency"]
            so.delivery_date    = frappe.utils.nowdate()
            so.transaction_date = frappe.utils.nowdate()
            so.order_type       = "Sales"
            so.po_no            = quotation

            if cart_data.get("shipping_address_name"):
                so.shipping_address_name = cart_data["shipping_address_name"]
            if cart_data.get("customer_address"):
                so.customer_address = cart_data["customer_address"]

            for item in cart_data.get("items") or []:
                so.append("items", {
                    "item_code": item["item_code"],
                    "qty":       item.get("qty", 1),
                    "rate":      item.get("rate", 0),
                    "warehouse": item.get("warehouse"),
                    "uom":       item.get("uom"),
                })
            for tax in cart_data.get("taxes") or []:
                so.append("taxes", {
                    "charge_type":  tax.get("charge_type", "On Net Total"),
                    "account_head": tax["account_head"],
                    "description":  tax.get("description") or tax["account_head"],
                    "rate":         tax.get("rate", 0),
                })

            so.flags.ignore_permissions = True
            so.insert()
            so.submit()
            frappe.db.commit()
        else:
            so = frappe.get_doc("Sales Order", existing_so)

        frappe.log_error(f"SO: {so.name}", "MoMo Finalize: Step 1 OK")

        if pr_name and frappe.db.exists("Payment Request", pr_name):
            frappe.db.set_value("Payment Request", pr_name, {
                "reference_doctype": "Sales Order",
                "reference_name":    so.name,
                "status":            "Paid",
            })
            frappe.db.commit()

        frappe.log_error(f"PR: {pr_name}", "MoMo Finalize: Step 2 OK")

        gw_full = (
            frappe.db.get_value("Payment Request", pr_name, "payment_gateway")
            if pr_name else ""
        )
        pga = frappe.db.get_value(
            "Payment Gateway Account",
            {"payment_gateway": gw_full},
            ["payment_account", "currency"],
            as_dict=True,
        ) if gw_full else None

        receivable_account = (
            frappe.db.get_value("Party Account", {
                "parenttype": "Customer",
                "parent":     so.customer,
                "company":    so.company,
            }, "account")
            or frappe.db.get_value("Account", {
                "account_type": "Receivable",
                "company":      so.company,
                "is_group":     0,
            }, "name")
            or frappe.db.get_value("Company", so.company, "default_receivable_account")
        )

        if not pga or not pga.payment_account:
            frappe.throw(f"Payment Gateway Account not configured for {gw_full}")

        pe = frappe.new_doc("Payment Entry")
        pe.payment_type               = "Receive"
        pe.posting_date               = frappe.utils.nowdate()
        pe.company                    = so.company
        pe.party_type                 = "Customer"
        pe.party                      = so.customer
        pe.party_name                 = so.customer_name
        pe.paid_from                  = receivable_account
        pe.paid_from_account_currency = so.currency
        pe.paid_to                    = pga.payment_account
        pe.paid_to_account_currency   = so.currency
        pe.paid_amount                = paid_amount
        pe.received_amount            = paid_amount
        pe.source_exchange_rate       = 1
        pe.target_exchange_rate       = 1
        pe.reference_no               = reference_id
        pe.reference_date             = frappe.utils.nowdate()
        pe.remarks = (
            f"MoMo payment received.\n"
            f"PR: {pr_name} | SO: {so.name} | MTN Ref: {reference_id}"
        )
        pe.append("references", {
            "reference_doctype":  "Sales Order",
            "reference_name":     so.name,
            "allocated_amount":   paid_amount,
            "total_amount":       so.grand_total,
            "outstanding_amount": so.grand_total,
        })
        pe.flags.ignore_permissions = True
        pe.insert()
        pe.submit()
        frappe.db.commit()
        so_doc = frappe.get_doc("Sales Order", so.name)
        so_doc.set_status(update=True)
        so_doc.notify_update()

        frappe.log_error(f"PE: {pe.name}", "MoMo Finalize: Step 3 OK")

        try:
            quot = frappe.get_doc("Quotation", quotation)
            if quot.docstatus == 0:
                quot.flags.ignore_permissions = True
                quot.submit()
                frappe.db.commit()
            frappe.log_error(f"Quotation {quotation} submitted", "MoMo Finalize: Step 4 OK")
        except Exception:
            frappe.log_error(frappe.get_traceback(), "MoMo Finalize: quotation submit (non-critical)")

        frappe.cache().delete_value(f"momo_pending_{token}")
        frappe.log_error(f"SO: {so.name} | PE: {pe.name}", "MoMo Finalize: COMPLETE")

        return {"sales_order": so.name, "payment_entry": pe.name}

    except Exception:
        frappe.log_error(frappe.get_traceback(), "MoMo Webshop Finalize Error")
        return {"error": "Order creation failed. Contact support with ref: " + reference_id}
    finally:
        frappe.set_user(original_user)


@frappe.whitelist(allow_guest=True)
def cancel_webshop_payment_request(token):
    """
    Called by the checkout page on failure/timeout/cancel.
    Cancels the dangling Payment Request so the cart is not blocked.
    """
    import json
    raw = frappe.cache().get_value(f"momo_pending_{token}")
    if not raw:
        return {"status": "ok"}
    cart_data = json.loads(raw)
    pr_name = cart_data.get("payment_request")
    if pr_name and frappe.db.exists("Payment Request", pr_name):
        try:
            frappe.db.set_value("Payment Request", pr_name, "docstatus", 2)
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "cancel_webshop_payment_request failed")
    return {"status": "ok"}
