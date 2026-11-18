from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('home', '0002_remove_customerprofile_location'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='phone_number',
            field=models.CharField(blank=True, max_length=20),
        ),
    ]
