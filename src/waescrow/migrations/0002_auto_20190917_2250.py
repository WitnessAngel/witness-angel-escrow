# Generated by Django 2.2.5 on 2019-09-17 20:50

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('waescrow', '0001_initial'),
    ]

    operations = [
        migrations.RenameField(
            model_name='escrowkeypair',
            old_name='key_pair',
            new_name='keypair',
        ),
    ]