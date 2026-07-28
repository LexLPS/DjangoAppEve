"""One account per email address, case-insensitive (threat model R10).

The form validates first; this expression index closes the race window and
enforces the rule for any non-form code path. Blank emails are exempt
(legacy accounts / createsuperuser without email).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_privacyactionlog"),
        # Must run after every auth migration: SQLite ALTERs rebuild
        # auth_user and would silently drop this raw-SQL index
        ("auth", "__latest__"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX accounts_user_email_ci_unique "
                "ON auth_user (LOWER(email)) WHERE email <> ''"
            ),
            reverse_sql="DROP INDEX accounts_user_email_ci_unique",
        ),
    ]
