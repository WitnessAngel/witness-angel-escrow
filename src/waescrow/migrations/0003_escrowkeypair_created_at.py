# Generated by Django 2.2.5 on 2019-09-17 21:23

import datetime
from django.db import migrations, models
from django.utils.timezone import utc


class Migration(migrations.Migration):

    dependencies = [
        ('waescrow', '0002_auto_20190917_2250'),
    ]

    operations = [
        migrations.AddField(
            model_name='escrowkeypair',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=datetime.datetime(2019, 9, 17, 21, 23, 19, 112594, tzinfo=utc)),
            preserve_default=False,
        ),
    ]
