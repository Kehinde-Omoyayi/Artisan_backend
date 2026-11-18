from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from home.phone_numbers import (
    calling_code_for_phone,
    national_input_for_phone,
    normalize_phone,
    phone_is_valid,
)


class PhoneNumberNormalizationTests(SimpleTestCase):
    def test_nigerian_local_number_with_or_without_trunk_zero_normalizes_to_e164(self):
        self.assertEqual(normalize_phone("0803 123 4567"), "+2348031234567")
        self.assertEqual(normalize_phone("803-123-4567"), "+2348031234567")

    def test_existing_international_number_is_validated_and_kept_in_e164(self):
        self.assertEqual(normalize_phone("+44 7400 123456"), "+447400123456")
        self.assertTrue(phone_is_valid("+447400123456"))
        self.assertEqual(calling_code_for_phone("+447400123456"), "+44")

    def test_invalid_length_and_invalid_number_patterns_are_rejected(self):
        for invalid in ("080312345", "+2341234567890", "not a phone number"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    normalize_phone(invalid)

    def test_blank_value_remains_optional_at_the_serializer_boundary(self):
        self.assertIsNone(normalize_phone(None))
        self.assertIsNone(normalize_phone("   "))
        self.assertFalse(phone_is_valid(""))

    def test_national_number_can_be_split_without_forcing_a_domestic_zero(self):
        self.assertEqual(
            national_input_for_phone("+2348031234567"),
            ("+234", "8031234567"),
        )
