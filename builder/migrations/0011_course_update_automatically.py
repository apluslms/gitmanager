# Generated by Django 2.2.24 on 2021-11-16 12:08

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('builder', '0010_course_email_on_error'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='update_automatically',
            field=models.BooleanField(default=True),
        ),
    ]
