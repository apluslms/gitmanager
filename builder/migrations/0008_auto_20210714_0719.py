# Generated by Django 2.2.24 on 2021-07-14 07:19

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('builder', '0007_auto_20210623_1222'),
    ]

    operations = [
        migrations.AlterField(
            model_name='course',
            name='git_origin',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
