from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("writeoffs", "0002_seed_writeoff_sequence")]

    operations = [
        migrations.AddField(
            model_name="writeoffdocument",
            name="business_author",
            field=models.CharField("Автор списания", max_length=150, blank=True),
        ),
    ]
