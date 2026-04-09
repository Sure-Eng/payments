frappe.ui.form.on("Payment Request", {
    onload: function (frm) {
        frm.momo_polling_interval = null;
        frm.momo_polling_count = 0;
        frm.momo_max_polls = 24; // 24 * 5s = 120 seconds
        frm.momo_cooldown_interval = null;

        frm.momo_start_polling = function () {
            if (frm.momo_polling_interval) return;
            frm.momo_polling_count = 0;
            frm.momo_polling_interval = setInterval(function () {
                frm.momo_polling_count++;
                frappe.db.get_list("Integration Request", {
                    filters: {
                        reference_doctype: "Payment Request",
                        reference_docname: frm.doc.name,
                    },
                    fields: ["name", "status"],
                    order_by: "creation desc",
                    limit: 1,
                }).then(function (results) {
                    if (!results || results.length === 0) return;
                    const ir_status = results[0].status;
                    if (ir_status === "Completed") {
                        frm.momo_stop_polling();
                        frm.reload_doc();
                        frappe.msgprint({
                            title: __("Payment Confirmed"),
                            message: __("MTN MoMo payment confirmed successfully. Payment Entry has been created."),
                            indicator: "green",
                        });
                    } else if (ir_status === "Failed") {
                        frm.momo_stop_polling();
                        frm.reload_doc();
                    }
                });
                if (frm.momo_polling_count >= frm.momo_max_polls) {
                    frm.momo_stop_polling();
                    frm.reload_doc();
                }
            }, 5000);
        };

        frm.momo_stop_polling = function () {
            if (frm.momo_polling_interval) {
                clearInterval(frm.momo_polling_interval);
                frm.momo_polling_interval = null;
            }
        };

        frm.momo_start_cooldown = function (seconds, btn) {
            // Disable the button and show countdown
            if (frm.momo_cooldown_interval) clearInterval(frm.momo_cooldown_interval);
            let remaining = seconds;
            btn.prop("disabled", true).text(__("Try Again ({0}s)", [remaining]));
            frm.momo_cooldown_interval = setInterval(function () {
                remaining--;
                if (remaining <= 0) {
                    clearInterval(frm.momo_cooldown_interval);
                    frm.momo_cooldown_interval = null;
                    btn.prop("disabled", false).text(__("Try Again"));
                } else {
                    btn.text(__("Try Again ({0}s)", [remaining]));
                }
            }, 1000);
        };
    },

    refresh: function (frm) {
        if (!(frm.doc.payment_gateway && frm.doc.payment_gateway.startsWith("MoMo-"))) {
            frm.set_df_property("phone_number", "reqd", 0);
            frm.set_df_property("email_to", "hidden", 0);
            return;
        }

        frm.set_df_property("email_to", "hidden", 1);
        frm.set_value("email_to", "");
        frm.set_df_property("phone_number", "reqd", 1);
        frm.set_df_property(
            "phone_number",
            "description",
            "MTN Cameroon number with country code, e.g. 237653096855"
        );
        if (!frm.doc.payment_channel || frm.doc.payment_channel !== "Phone") {
            frm.set_value("payment_channel", "Phone");
        }

        if (frm.doc.docstatus === 1 && frm.doc.status === "Requested") {
            frm.dashboard.set_headline_alert(
                __("MTN MoMo payment request sent to {0}. Waiting for customer to confirm.", [frm.doc.phone_number]),
                "blue"
            );
            frm.momo_start_polling();
        }

        if (frm.doc.docstatus === 1 && frm.doc.status === "Paid") {
            frm.dashboard.set_headline_alert(
                __("Payment confirmed. MoMo transaction completed successfully."),
                "green"
            );
        }

        if (frm.doc.docstatus === 1 && frm.doc.status === "Failed") {
            frm.dashboard.set_headline_alert(
                __("Payment failed or was declined by the customer on {0}.", [frm.doc.phone_number]),
                "red"
            );

            frm.add_custom_button(__("Try Again"), function () {
                const btn = frm.get_field ? $(frm.page.btn_secondary) : $();
                frappe.confirm(
                    __("Resend the MTN MoMo payment prompt to {0}?", [frm.doc.phone_number]),
                    function () {
                        frappe.call({
                            method: "payments.payment_gateways.doctype.momo_settings.momo_settings.retry_payment_request",
                            args: { payment_request_name: frm.doc.name },
                            freeze: true,
                            freeze_message: __("Resending payment request to MTN..."),
                            callback: function (r) {
                                if (!r.exc) {
                                    frappe.msgprint({
                                        title: __("Payment Request Resent"),
                                        message: __("A new MTN MoMo prompt has been sent to <strong>{0}</strong>. Please ask the customer to confirm.", [frm.doc.phone_number]),
                                        indicator: "blue",
                                    });
                                    frm.reload_doc();
                                } else {
                                    // Start 60s cooldown on the button if server threw Too Soon
                                    const try_again_btn = frm.custom_buttons[__("Try Again")];
                                    if (try_again_btn) {
                                        frm.momo_start_cooldown(60, try_again_btn);
                                    }
                                }
                            },
                        });
                    }
                );
            }, __("MoMo"));

            // Auto-start 60s cooldown if last IR failed very recently
            frappe.db.get_list("Integration Request", {
                filters: {
                    reference_doctype: "Payment Request",
                    reference_docname: frm.doc.name,
                    status: "Failed",
                },
                fields: ["modified"],
                order_by: "creation desc",
                limit: 1,
            }).then(function (results) {
                if (!results || results.length === 0) return;
                const last_modified = new Date(results[0].modified + " UTC");
                const now = new Date();
                const seconds_since = Math.floor((now - last_modified) / 1000);
                const remaining = 60 - seconds_since;
                if (remaining > 0) {
                    const try_again_btn = frm.custom_buttons[__("Try Again")];
                    if (try_again_btn) {
                        frm.momo_start_cooldown(remaining, try_again_btn);
                    }
                }
            });
        }
    },

    payment_gateway: function (frm) {
        if (frm.doc.payment_gateway && frm.doc.payment_gateway.startsWith("MoMo-")) {
            frm.set_df_property("email_to", "hidden", 1);
            frm.set_value("email_to", "");
            frm.set_df_property("phone_number", "reqd", 1);
            frm.set_df_property(
                "phone_number",
                "description",
                "MTN Cameroon number with country code, e.g. 237653096855"
            );
            frm.set_value("payment_channel", "Phone");
        } else {
            frm.set_df_property("phone_number", "reqd", 0);
            frm.set_df_property("email_to", "hidden", 0);
        }
    },

    after_save: function (frm) {
        if (
            frm.doc.payment_gateway &&
            frm.doc.payment_gateway.startsWith("MoMo-") &&
            frm.doc.docstatus === 1 &&
            frm.doc.status === "Requested"
        ) {
            frappe.msgprint({
                title: __("Payment Request Sent"),
                message: __("An MTN MoMo payment prompt has been sent to <strong>{0}</strong>. Please ask the customer to check their phone and enter their PIN to confirm payment.", [frm.doc.phone_number]),
                indicator: "blue",
            });
        }
    },
});
