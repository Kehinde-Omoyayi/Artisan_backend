from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('home', '0003_customuser_phone_number'),
    ]

    operations = [
        migrations.AlterField(
            model_name='customuser',
            name='phone_number',
            field=models.CharField(blank=True, max_length=20, unique=True),
        ),
    ]
