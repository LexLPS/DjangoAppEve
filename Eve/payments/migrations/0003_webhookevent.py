from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0002_order_idempotency_key_order_saleor_checkout_id_and_more")]

    operations = [
        migrations.CreateModel(
            name="WebhookEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fingerprint", models.CharField(max_length=64, unique=True)),
                ("event_type", models.CharField(max_length=64)),
                ("saleor_order_id", models.CharField(max_length=100)),
                ("payload", models.JSONField()),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processed", "Processed"), ("ignored", "Ignored"), ("rejected", "Rejected")], db_index=True, default="pending", max_length=16)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("last_error", models.CharField(blank=True, default="", max_length=100)),
                ("received_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"indexes": [models.Index(fields=["status", "received_at"], name="payments_we_status_2225cf_idx")]},
        ),
    ]
