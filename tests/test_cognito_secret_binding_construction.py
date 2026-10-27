import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CognitoSecretBindingConstructionTests(unittest.TestCase):
    def test_raw_fn_sub_preserves_array_and_double_encoding_is_detectable(self):
        document = json.loads(
            (ROOT / "infrastructure" / "novamind-cognito-components.template.json").read_text(encoding="utf-8")
        )
        expression = document["Resources"]["CognitoConfigurationSecret"]["Properties"]["SecretString"]["Fn::Sub"]
        self.assertIsInstance(expression, list)
        template, substitutions = expression
        self.assertIn('"customer_bindings":${Bindings}', template)
        self.assertNotIn('"customer_bindings":"${Bindings}"', template)
        self.assertEqual(document["Transform"], "AWS::LanguageExtensions")
        self.assertEqual(substitutions["Bindings"], {"Fn::ToJsonString": {"Ref": "CustomerBindingsJson"}})

        bindings = (
            '[{"subject_id":"6498e4d8-1071-7060-529e-78d45ffd368d",'
            '"customer_id":"26f2402e-a341-4cef-9abf-d057889b33ab"},'
            '{"subject_id":"f4087468-9051-7045-f923-5d07362fbfb7",'
            '"customer_id":"9c9d5a45-e08f-4076-acfd-f80c3bc8b1e3"}]'
        )
        values = {
            "AWS::Region": "us-east-1",
            "PoolId": "us-east-1_SYNTHETIC",
            "ClientId": "syntheticclient",
            "CustomerGroup": "customers",
            "ReviewerGroup": "reviewers",
            "AdminGroup": "admins",
            "Bindings": json.dumps(bindings),
        }
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace("${" + key + "}", value)
        parsed = json.loads(rendered)
        self.assertIsInstance(parsed["customer_bindings"], str)
        self.assertEqual(len(json.loads(parsed["customer_bindings"])), 2)

        double_encoded = template
        for key, value in values.items():
            replacement = json.dumps(value) if key == "Bindings" else value
            double_encoded = double_encoded.replace("${" + key + "}", replacement)
        parsed_double = json.loads(double_encoded)
        self.assertIsInstance(parsed_double["customer_bindings"], str)

    def test_ecs_json_key_projection_of_array_objects_is_not_json(self):
        # ECS agent JSON-key extraction uses fmt.Sprintf("%v", value). For a
        # []interface{} containing maps, that is Go debug formatting, not JSON.
        projected_array = "[map[customer_id:synthetic-customer subject_id:synthetic-subject]]"
        with self.assertRaises(json.JSONDecodeError):
            json.loads(projected_array)

        # If the outer secret stores the array as a JSON string, the extracted
        # value remains the inner JSON text and the application parser accepts it.
        inner_json = '[{"subject_id":"synthetic-subject","customer_id":"synthetic-customer"}]'
        self.assertEqual(json.loads(inner_json)[0]["subject_id"], "synthetic-subject")


if __name__ == "__main__":
    unittest.main()
