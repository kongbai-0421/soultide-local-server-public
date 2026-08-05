import os
import unittest

os.environ.setdefault("SOULTIDE_PAYMENT_MODE", "local")

import sdk_server


class SdkPaymentBoundaryTests(unittest.TestCase):
    def test_local_sdk_does_not_claim_provider_success(self):
        status, response = sdk_server._payment_response("/pay/order")
        self.assertEqual(status, 409)
        self.assertEqual(response["data"]["mode"], "local")
        self.assertEqual(response["data"]["status"], "use_local_order_grant")
        self.assertIsNone(response["data"]["providerReceipt"])
        self.assertNotIn("payResult", response["data"])
        self.assertNotIn("orderId", response["data"])


if __name__ == "__main__":
    unittest.main()
