from io import BytesIO
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from home.models import ArtisanProfile, CustomUser
from .serializers import ArtisanProfileSerializer


def image_bytes(image_format, color):
    output = BytesIO()
    Image.new('RGB', (32, 32), color).save(output, format=image_format)
    return output.getvalue()


class ArtisanProfilePhotoReplacementTests(TestCase):
    def make_artisan(self, email, first_name):
        user = CustomUser.objects.create_user(
            email=email,
            first_name=first_name,
            last_name='Artisan',
            password='StrongPassword123!',
            is_active=True,
        )
        return ArtisanProfile.objects.create(user=user, business_name=f'{first_name} Services')

    def test_old_photo_is_retained_until_commit_then_removed_when_unshared(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            artisan = self.make_artisan('photo-owner@example.test', 'Ari')
            artisan.profile_photo.save('original.webp', ContentFile(image_bytes('WEBP', (10, 20, 30))), save=True)
            old_name = artisan.profile_photo.name
            storage = artisan.profile_photo.storage
            uploaded = SimpleUploadedFile('replacement.png', image_bytes('PNG', (30, 20, 10)), content_type='image/png')
            serializer = ArtisanProfileSerializer(artisan, data={'profile_photo': uploaded}, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)

            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                updated = serializer.save()
                new_name = updated.profile_photo.name
                self.assertNotEqual(new_name, old_name)
                self.assertTrue(storage.exists(old_name))
                self.assertTrue(storage.exists(new_name))

            self.assertGreaterEqual(len(callbacks), 1)
            for callback in callbacks:
                callback()
            self.assertFalse(storage.exists(old_name))
            self.assertTrue(storage.exists(new_name))

    def test_shared_old_photo_is_not_deleted_when_one_profile_replaces_it(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            artisan = self.make_artisan('first-owner@example.test', 'Ari')
            artisan.profile_photo.save('shared.webp', ContentFile(image_bytes('WEBP', (10, 20, 30))), save=True)
            old_name = artisan.profile_photo.name
            storage = artisan.profile_photo.storage
            second_artisan = self.make_artisan('second-owner@example.test', 'Bola')
            second_artisan.profile_photo = old_name
            second_artisan.save(update_fields=['profile_photo'])

            uploaded = SimpleUploadedFile('replacement.png', image_bytes('PNG', (30, 20, 10)), content_type='image/png')
            serializer = ArtisanProfileSerializer(artisan, data={'profile_photo': uploaded}, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)

            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                serializer.save()
            self.assertGreaterEqual(len(callbacks), 1)
            for callback in callbacks:
                callback()

            self.assertTrue(storage.exists(old_name))
            self.assertEqual(ArtisanProfile.objects.get(pk=second_artisan.pk).profile_photo.name, old_name)

    def test_invalid_replacement_leaves_the_old_photo_untouched(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            artisan = self.make_artisan('invalid-photo@example.test', 'Ari')
            artisan.profile_photo.save('original.webp', ContentFile(image_bytes('WEBP', (10, 20, 30))), save=True)
            old_name = artisan.profile_photo.name
            storage = artisan.profile_photo.storage
            uploaded = SimpleUploadedFile('replacement.png', b'not an image', content_type='image/png')
            serializer = ArtisanProfileSerializer(artisan, data={'profile_photo': uploaded}, partial=True)

            self.assertFalse(serializer.is_valid())
            self.assertTrue(storage.exists(old_name))
            self.assertEqual(ArtisanProfile.objects.get(pk=artisan.pk).profile_photo.name, old_name)

    def test_database_save_failure_keeps_old_photo_and_removes_staged_new_photo(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            artisan = self.make_artisan('save-failure@example.test', 'Ari')
            artisan.profile_photo.save('original.webp', ContentFile(image_bytes('WEBP', (10, 20, 30))), save=True)
            old_name = artisan.profile_photo.name
            storage = artisan.profile_photo.storage
            uploaded = SimpleUploadedFile('replacement.png', image_bytes('PNG', (30, 20, 10)), content_type='image/png')
            serializer = ArtisanProfileSerializer(artisan, data={'profile_photo': uploaded}, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)

            with patch.object(ArtisanProfile, 'save', side_effect=RuntimeError('database write failed')):
                with self.assertRaisesMessage(RuntimeError, 'database write failed'):
                    serializer.save()

            self.assertTrue(storage.exists(old_name))
            self.assertEqual(ArtisanProfile.objects.get(pk=artisan.pk).profile_photo.name, old_name)
            directory, old_basename = old_name.rsplit('/', 1)
            subdirectories, files = storage.listdir(directory)
            self.assertEqual(set(files), {old_basename})
