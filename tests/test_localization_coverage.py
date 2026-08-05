import unittest

from tools.audit_localization_coverage import effect_audit, handler_audit


class LocalizationCoverageAuditTests(unittest.TestCase):
    def test_zero_effect_and_dynamic_values_are_classified_as_sentinels(self):
        result = effect_audit(
            {
                "buffs": {
                    "one": {
                        "effectTypes": [0, 101],
                        "dynamicArgType": [0, 102, 204],
                    }
                }
            },
            "effect_type == 101",
        )

        self.assertEqual(result["effect_types"]["0"]["status"], "sentinel")
        self.assertEqual(
            result["dynamic_arg_types"]["0"]["status"],
            "constant_or_default",
        )
        self.assertEqual(result["dynamic_arg_types"]["102"]["status"], "executed_branch")
        self.assertEqual(result["unsupported_dynamic_arg_types"], [])

    def test_unknown_effect_remains_unsupported(self):
        result = effect_audit({"effectTypes": [999]}, "")
        self.assertEqual(result["effect_types"]["999"]["status"], "unsupported")

    def test_all_catalog_requests_have_explicit_or_rule_dispatch(self):
        report = handler_audit()
        self.assertEqual(report["request_classes"].get("tcp_or_typed_fallback", 0), 0)
        self.assertEqual(report["remaining_action_count"], len(report["remaining_action_ids"]))
        for message_id in (1002, 1003, 1702, 1705, 3702, 7107):
            self.assertNotIn(message_id, report["remaining_action_ids"])


if __name__ == "__main__":
    unittest.main()
