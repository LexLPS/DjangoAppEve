"""Remove DRF's plaintext token table (threat model R11).

`rest_framework.authtoken` is no longer installed; its table stored token
values in plaintext with no expiry, so any row left behind would remain a
usable credential. Dropping it is the point of the change, not a
side effect.

Irreversible by design: recreating the table would restore the weakness,
and any tokens it held are intentionally destroyed. Clients re-authenticate
against /api/v1/auth/token/ to obtain a hashed, expiring token.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS authtoken_token",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
