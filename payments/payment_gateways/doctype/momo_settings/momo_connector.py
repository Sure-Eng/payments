"""
MomoConnector - MTN Mobile Money Collection API client.
Mirrors the MpesaConnector pattern.
"""

import base64
import uuid
import requests


class MomoConnector:
    SANDBOX_URL = "https://sandbox.momodeveloper.mtn.com"
    PRODUCTION_URL = "https://proxy.momoapi.mtn.com"

    def __init__(self, env="sandbox", api_user_id=None, api_key=None,
                 subscription_key=None, target_environment="sandbox"):
        self.env = env
        self.api_user_id = api_user_id
        self.api_key = api_key
        self.subscription_key = subscription_key
        self.target_environment = target_environment
        self.base_url = self.SANDBOX_URL if env == "sandbox" else self.PRODUCTION_URL
        self.access_token = None
        self.authenticate()

    # -- Authentication -------------------------------------------------------

    def authenticate(self):
        url = f"{self.base_url}/collection/token/"
        credentials = base64.b64encode(
            f"{self.api_user_id}:{self.api_key}".encode()
        ).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Ocp-Apim-Subscription-Key": self.subscription_key,
        }
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        self.access_token = response.json()["access_token"]
        return self.access_token

    # -- Collection endpoints -------------------------------------------------

    def request_to_pay(self, amount, currency, payer_msisdn, external_id,
                       callback_url, payer_message="Payment",
                       payee_note="Thank you"):
        reference_id = str(uuid.uuid4())
        url = f"{self.base_url}/collection/v1_0/requesttopay"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Reference-Id": reference_id,
            "X-Target-Environment": self.target_environment,
            "Ocp-Apim-Subscription-Key": self.subscription_key,
            "X-Callback-Url": callback_url,
            "Content-Type": "application/json",
        }
        payload = {
            "amount": str(int(float(amount))),
            "currency": currency,
            "externalId": external_id or reference_id,
            "payer": {
                "partyIdType": "MSISDN",
                "partyId": payer_msisdn,
            },
            "payerMessage": payer_message,
            "payeeNote": payee_note,
        }

        import frappe
        frappe.logger().info(f"MoMo requesttopay payload: {payload}")
        frappe.logger().info(f"MoMo requesttopay headers (safe): X-Reference-Id={reference_id}, X-Target-Environment={self.target_environment}, currency={currency}, amount={payload['amount']}")

        response = requests.post(url, headers=headers, json=payload)

        frappe.logger().info(f"MoMo response status: {response.status_code}")
        frappe.logger().info(f"MoMo response body: {response.text}")

        if response.status_code == 202:
            return {"referenceId": reference_id, "status": "PENDING"}

        # Log full response before raising so we can see MTN's error body
        frappe.log_error(
            f"MTN API Error\nStatus: {response.status_code}\nBody: {response.text}\nPayload: {payload}",
            "MoMo requesttopay Failed"
        )
        response.raise_for_status()

    def get_transaction_status(self, reference_id):
        url = f"{self.base_url}/collection/v1_0/requesttopay/{reference_id}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Target-Environment": self.target_environment,
            "Ocp-Apim-Subscription-Key": self.subscription_key,
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
