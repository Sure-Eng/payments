// MoMo Settings -- form controller
frappe.ui.form.on("MoMo Settings", {
    refresh: function (frm) {
        frm.set_df_property("callback_url", "read_only", 1);

        if (!frm.doc.__islocal) {
            // -- Test Connection button
            frm.add_custom_button(__("Test Connection"), function () {
                frappe.confirm(
                    __("This will make a live API call to MTN MoMo to verify your credentials. Continue?"),
                    function () {
                        frappe.call({
                            method:
                                "payments.payment_gateways.doctype.momo_settings.momo_settings.poll_transaction_status",
                            args: {
                                reference_id: "00000000-0000-0000-0000-000000000000",
                                gateway_name: frm.doc.name,
                            },
                            freeze: true,
                            freeze_message: __("Connecting to MTN MoMo API..."),
                            callback: function (r) {
                                if (!r.exc) {
                                    frappe.msgprint({
                                        title: __("Connection Successful"),
                                        message: __("MTN MoMo credentials are valid. Response: ")
                                            + JSON.stringify(r.message || {}),
                                        indicator: "green",
                                    });
                                }
                            },
                            error: function (r) {
                                frappe.msgprint({
                                    title: __("Connection Failed"),
                                    message: __(
                                        "Could not connect to MTN MoMo. Check API User ID, API Key, and Subscription Key. See Error Log for details."
                                    ),
                                    indicator: "red",
                                });
                            },
                        });
                    }
                );
            }, __("MTN MoMo"));
        }

        // Sandbox warning banner
        if (frm.doc.use_sandbox) {
            frm.dashboard.set_headline_alert(
                __(
                    "Sandbox mode is ON. Payments are not real. "
                    + "Switch off 'Use Sandbox' and set Target Environment for production."
                ),
                "orange"
            );
        }
    },

    use_sandbox: function (frm) {
        if (frm.doc.use_sandbox) {
            frm.set_value("target_environment", "sandbox");
        }
    },
});
