"""A phone number can never again stop a profile from being saved.

The report: after an image was rejected, the artisan could not update their
profile at all. The API answered 409 "the request conflicts with the current
state of the resource" on every save, and the traceback was a UNIQUE violation
on ``home_customuser.phone_number``.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from rest_framework import serializers

from profiles.serializers import _save_profile_user, _phone_in_use

User = get_user_model()


class SaveOnlyWhatChangedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="artisan-phone@example.com",
            password="Review12345!",
            first_name="test",
            last_name="two",
        )
        User.objects.filter(pk=self.user.pk).update(phone_number=None)
        self.user.refresh_from_db()

    def test_an_untouched_phone_number_is_not_written(self):
        """Saving a bio must not touch the unique phone column at all."""
        with mock.patch.object(User, "save", autospec=True) as save:
            _save_profile_user(self.user, {"first_name": "test", "phone_number": None})
        self.assertFalse(save.called, "an unchanged phone number was written again")

    def test_a_changed_name_is_written_alone(self):
        with mock.patch.object(User, "save", autospec=True) as save:
            _save_profile_user(self.user, {"first_name": "Tunde"})
        self.assertTrue(save.called)
        self.assertEqual(save.call_args.kwargs["update_fields"], ["first_name", "updated_at"])
        self.assertEqual(self.user.first_name, "Tunde")

    def test_blank_and_null_are_the_same_number(self):
        User.objects.filter(pk=self.user.pk).update(phone_number="")
        self.user.refresh_from_db()
        with mock.patch.object(User, "save", autospec=True) as save:
            _save_profile_user(self.user, {"phone_number": None})
        self.assertFalse(save.called, "blank vs NULL was treated as an edit")

    def test_a_real_clash_names_the_field_instead_of_409(self):
        def boom(*args, **kwargs):
            raise IntegrityError("UNIQUE constraint failed: home_customuser.phone_number")

        with mock.patch.object(User, "save", boom):
            with self.assertRaises(serializers.ValidationError) as caught:
                _save_profile_user(self.user, {"phone_number": "+2348055554444"})
        self.assertIn("phone_number", caught.exception.detail)
        message = str(caught.exception.detail["phone_number"][0])
        self.assertIn("already registered", message)


class PhoneValidationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com", password="Review12345!", first_name="Own", last_name="Er"
        )
        User.objects.filter(pk=self.owner.pk).update(phone_number="+2348012345678")
        self.other = User.objects.create_user(
            email="other@example.com", password="Review12345!", first_name="Oth", last_name="Er"
        )
        User.objects.filter(pk=self.other.pk).update(phone_number="+2348099998888")

    def test_a_number_someone_else_owns_is_refused(self):
        self.assertTrue(_phone_in_use("+2348099998888", self.other) is False)
        self.assertTrue(_phone_in_use("+2348099998888", self.owner) is True)

    def test_your_own_number_is_not_a_clash(self):
        self.assertFalse(_phone_in_use("+2348012345678", self.owner))
