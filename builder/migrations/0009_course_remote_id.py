# Generated by Django 2.2.24 on 2021-09-16 10:25

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('builder', '0008_auto_20210714_0719'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='remote_id',
            field=models.IntegerField(blank=True, null=True, unique=True),
        ),
    ]
