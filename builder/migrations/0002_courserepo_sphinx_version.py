# Generated by Django 2.2.23 on 2021-05-25 12:38

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('builder', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='courserepo',
            name='sphinx_version',
            field=models.CharField(choices=[('old', 'old'), ('new', 'new')], default='old', max_length=3),
        ),
    ]
