from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ('home', '0001_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='customerprofile',
            name='location',
        ),
    ]
