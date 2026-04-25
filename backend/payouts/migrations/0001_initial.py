from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Merchant',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('email', models.EmailField(unique=True)),
                ('bank_account_id', models.CharField(max_length=64, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='Payout',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amount_paise', models.BigIntegerField()),
                ('bank_account_id', models.CharField(max_length=64)),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('processing', 'Processing'),
                             ('completed', 'Completed'), ('failed', 'Failed')],
                    default='pending', max_length=16
                )),
                ('attempt_count', models.IntegerField(default=0)),
                ('max_attempts', models.IntegerField(default=3)),
                ('failure_reason', models.TextField(blank=True)),
                ('processing_started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('merchant', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='payouts', to='payouts.merchant'
                )),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='LedgerEntry',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amount_paise', models.BigIntegerField()),
                ('entry_type', models.CharField(
                    choices=[('credit', 'Credit'), ('debit', 'Debit')], max_length=6
                )),
                ('description', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('merchant', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='ledger_entries', to='payouts.merchant'
                )),
                ('payout', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='ledger_entries', to='payouts.payout'
                )),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='IdempotencyKey',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('key', models.CharField(max_length=255)),
                ('response_status', models.IntegerField()),
                ('response_body', models.JSONField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField()),
                ('merchant', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, to='payouts.merchant'
                )),
                ('payout', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='idempotency_keys', to='payouts.payout'
                )),
            ],
        ),
        migrations.AddIndex(
            model_name='merchant',
            index=models.Index(fields=['email'], name='payouts_mer_email_idx'),
        ),
        migrations.AddIndex(
            model_name='ledgerentry',
            index=models.Index(fields=['merchant', 'created_at'], name='payouts_led_merch_idx'),
        ),
        migrations.AddIndex(
            model_name='payout',
            index=models.Index(fields=['merchant', 'status'], name='payouts_pay_merch_status_idx'),
        ),
        migrations.AddIndex(
            model_name='payout',
            index=models.Index(fields=['status', 'processing_started_at'], name='payouts_pay_status_proc_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='idempotencykey',
            unique_together={('merchant', 'key')},
        ),
        migrations.AddIndex(
            model_name='idempotencykey',
            index=models.Index(fields=['merchant', 'key'], name='payouts_idem_merch_key_idx'),
        ),
    ]
